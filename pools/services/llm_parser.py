import base64
import io
import json
import logging
import os
from datetime import date
from pathlib import Path

import anthropic
from PIL import Image

logger = logging.getLogger(__name__)


# Formats Claude's vision API accepts directly.
_CLAUDE_SUPPORTED = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}

# How much source text parse_all_pools works from. Larger than the single-pool
# paths use, because it extracts every pool on a page rather than one.
#
# Callers must ask fetch_url for this much: its own default is smaller, and it
# truncates before this function ever sees the text, so a caller that leaves the
# default in place silently gets the smaller limit and drops pools off the end of
# a long page with no error. Exported so the call site can name it rather than
# repeat the number.
ALL_POOLS_MAX_CHARS = 12000


def _prepare_image_for_claude(image_bytes: bytes) -> tuple[bytes, str]:
    """Return (bytes, media_type) suitable for the Claude vision API.

    Detects format from bytes (not filename). Converts unsupported formats
    (e.g. AVIF, HEIF) to JPEG so they can be sent to Claude.
    """
    img = Image.open(io.BytesIO(image_bytes))
    fmt = img.format or "JPEG"
    if fmt in _CLAUDE_SUPPORTED:
        return image_bytes, _CLAUDE_SUPPORTED[fmt]
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=92)
    return buf.getvalue(), "image/jpeg"


def _response_text(message) -> str:
    """Join the text blocks of a Claude response, skipping any thinking blocks.

    content[0] is not reliably the answer: models that think return a
    ThinkingBlock ahead of the text, and Sonnet 5 thinks adaptively whenever the
    `thinking` parameter is omitted — so the same call can come back either way.
    """
    return "".join(block.text for block in message.content if block.type == "text")


def _no_text_note(message) -> str:
    """Describe a response that carried no text, for storing in place of "".

    Most often this means thinking consumed the whole max_tokens budget. Storing
    an empty string leaves the admin rendering its empty-value dash, which looks
    exactly like "the parse never ran" — the reviewer has no way to tell the two
    apart.
    """
    blocks = ", ".join(sorted({block.type for block in message.content})) or "none"
    return f"(no text in response — stop_reason={message.stop_reason}, blocks=[{blocks}])"


def _system_prompt() -> str:
    today = date.today()
    return (
        "You are a data extraction assistant for Philadelphia public swimming pools.\n"
        "Extract pool schedule information from the provided content and return ONLY valid JSON.\n"
        "Use null for fields you cannot determine. Dates must be YYYY-MM-DD format.\n"
        "Pool IDs come from the provided list; set pool_id to null if unsure.\n"
        "Match pool_id by direct name similarity only. Do not use your knowledge of Philadelphia "
        "geography, street names, or neighborhood associations to override or reassign a name match. "
        "If a name in the source closely matches a name in the pool list, use that match — do not "
        "substitute a different pool based on location or association.\n"
        f"Today's date is {today}. If a date in the content has no year, assume it is {today.year}.\n"
        f"Set stale_year_warning to true if the content appears to be from a prior season "
        f"(e.g. references {today.year - 1} or earlier dates as current)."
    )

_SCHEDULE_INSTRUCTIONS = """\
weekday_schedule covers Monday through Friday ONLY. weekend_schedule covers Saturday and Sunday ONLY.
Never include Sat or Sun data in weekday_schedule. Never include Mon–Fri data in weekend_schedule.
For weekday_schedule and weekend_schedule, summarize into compact time blocks, one per line.
Format each line as: "H–H Activity" using an en-dash (e.g. "11–1 Camp Swim" or "1–4 Open Swim").
Use 12-hour times without am/pm unless needed for clarity.
Merge consecutive blocks with the same activity into one span (e.g. "11–12 Day Camp" + "12–1 Day Camp" → "11–1 Day Camp").
Also treat gaps of 10 minutes or less between same-activity blocks as continuous and merge them (e.g. "1–1:50 Open Swim" + "2–2:50 Open Swim" → "1–2:50 Open Swim").
Note per-day variations in parentheses only when days within that section differ (e.g. "4–5 Swim Lessons (Mon, Wed) / Swim Team (Tue, Thu)" in weekday_schedule).
Do NOT annotate with day names when an activity applies to every day in that section.
Omit any "pool closed" or "no activity" blocks at the start or end of the day — those are implied by the hours listed.
Do include a "closed" block only if there is a gap of more than 10 minutes in the middle of an otherwise active day (e.g. closed 12–1 between two sessions).
If weekday and weekend schedules are identical, still fill in both fields."""

_PROMPT_TEMPLATE = """Pool list (id: name — address):
{pool_list}

Content to analyze:
{content}

{schedule_instructions}

Return JSON with exactly these fields:
{{
  "pool_id": <integer or null>,
  "opening_date": "<YYYY-MM-DD or null>",
  "closing_date": "<YYYY-MM-DD or null>",
  "weekday_schedule": "<summarized weekday time blocks, one per line, or null>",
  "weekend_schedule": "<summarized weekend time blocks, one per line, or null>",
  "notes": "<any other relevant info or null>",
  "confidence": "<high|medium|low>",
  "stale_year_warning": <true|false>
}}"""

_IMAGE_PROMPT = """Pool list (id: name — address):
{pool_list}

The image above is a screenshot (e.g. from Instagram or a website) with Philadelphia pool schedule information.
OCR the text in the image and extract pool schedule data.

{schedule_instructions}

Return JSON with exactly these fields:
{{
  "pool_id": <integer or null>,
  "opening_date": "<YYYY-MM-DD or null>",
  "closing_date": "<YYYY-MM-DD or null>",
  "weekday_schedule": "<summarized weekday time blocks, one per line, or null>",
  "weekend_schedule": "<summarized weekend time blocks, one per line, or null>",
  "notes": "<any other relevant info or null>",
  "confidence": "<high|medium|low>",
  "stale_year_warning": <true|false>
}}"""


def moderate_image(image_bytes: bytes, image_name: str) -> bool:
    """Return True if the image should be rejected (nudity or illegal content).
    Fails open: returns False if the API call fails, so legitimate submissions
    aren't blocked by a transient API outage.
    Files named FLAGME_* are always rejected — useful for testing the flow."""
    if os.path.basename(image_name).startswith("FLAGME_"):
        return True

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    image_bytes, media_type = _prepare_image_for_claude(image_bytes)
    image_data = base64.standard_b64encode(image_bytes).decode("utf-8")

    try:
        message = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=64,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": image_data},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Does this image contain nudity, explicit sexual content, or illegal content? "
                            "People wearing swimsuits, swim trunks, bikinis, or similar swimming attire are NOT nudity — "
                            "this is a pool scheduling app and swimwear is expected and fine. "
                            'Reply with only JSON: {"flagged": true} or {"flagged": false}'
                        ),
                    },
                ],
            }],
        )
        result = json.loads(_response_text(message).strip())
        return bool(result.get("flagged"))
    except Exception:
        return False


def build_pool_list() -> list[dict]:
    """Build the pool list (with alternate names) to pass to LLM parse functions."""
    from pools.models import Pool
    return [
        {
            "id": p.id,
            "name": p.name,
            "address": p.address,
            "alternate_names": [a.name for a in p.alternate_names.all()],
        }
        for p in Pool.objects.prefetch_related("alternate_names").all()
    ]


def _format_pool_list(pool_list: list[dict]) -> str:
    lines = []
    for p in pool_list:
        line = f"{p['id']}: {p['name']}"
        alts = p.get("alternate_names", [])
        if alts:
            line += f" (also known as: {', '.join(alts)})"
        if p.get("address"):
            line += f" — {p['address']}"
        lines.append(line)
    return "\n".join(lines)


_ALL_POOLS_PROMPT = """Pool list (id: name — address):
{pool_list}

Content to analyze:
{content}

This source may mention multiple Philadelphia pools. Extract ALL pools mentioned with schedule info.
Do not put address information or address discrepancies in the notes field — addresses in the source
may legitimately differ from the pool list and that difference is not relevant here.
Return a JSON array — one object per pool found:
[
  {{
    "pool_id": <integer from the pool list above, or null if no match>,
    "pool_name": "<name as written in the source>",
    "opening_date": "<YYYY-MM-DD or null>",
    "closing_date": "<YYYY-MM-DD or null>",
    "phone_number": "<phone number for this pool as shown in the source, or null>",
    "notes": "<free-text to append to this pool's updates, e.g. fitness class schedules (Aqua Zumba, Water Aerobics, etc.) with days/times/start dates — exclude address info — or null>"
  }}
]
Return [] if no pool schedule info is found."""

_ALL_POOLS_IMAGE_PROMPT = """Pool list (id: name — address):
{pool_list}

The image(s) above may mention multiple Philadelphia pools. OCR the text and extract ALL pools
mentioned with schedule info.
Do not put address information or address discrepancies in the notes field — addresses in the source
may legitimately differ from the pool list and that difference is not relevant here.
Return a JSON array — one object per pool found:
[
  {{
    "pool_id": <integer from the pool list above, or null if no match>,
    "pool_name": "<name as written in the source>",
    "opening_date": "<YYYY-MM-DD or null>",
    "closing_date": "<YYYY-MM-DD or null>",
    "phone_number": "<phone number for this pool as shown in the source, or null>",
    "notes": "<free-text to append to this pool's updates, e.g. fitness class schedules (Aqua Zumba, Water Aerobics, etc.) with days/times/start dates — exclude address info — or null>"
  }}
]
Return [] if no pool schedule info is found."""


def _parse_response(response_text: str) -> dict:
    text = response_text.strip()
    start = text.find("{")
    if start == -1:
        raise json.JSONDecodeError("No JSON object found", text, 0)
    result, _ = json.JSONDecoder().raw_decode(text, start)
    return result


def _parse_list_response(response_text: str) -> list:
    text = response_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
        text = text.strip()
    start = text.find("[")
    if start == -1:
        return []
    result, _ = json.JSONDecoder().raw_decode(text, start)
    return result if isinstance(result, list) else []


def parse_submission(text: str, pool_list: list[dict]) -> dict:
    """Parse text content (fetched from a URL) for pool schedule info."""
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    prompt = _PROMPT_TEMPLATE.format(
        pool_list=_format_pool_list(pool_list),
        content=text[:8000],
        schedule_instructions=_SCHEDULE_INSTRUCTIONS,
    )
    message = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=8192,
        system=_system_prompt(),
        messages=[{"role": "user", "content": prompt}],
    )
    raw = _response_text(message) or _no_text_note(message)
    try:
        result = _parse_response(raw)
    except (json.JSONDecodeError, ValueError):
        return {"_raw": raw}
    result["_raw"] = raw
    return result


_HEAT_EMERGENCY_PROMPT = """\
You are reading a press release from the Philadelphia Department of Public Health about a \
Heat Health Emergency. These releases come in several forms:
1. Declaring a new emergency, e.g. "Health Department Declares Heat Health Emergency \
Wednesday, July 1 at 11 a.m. through Saturday, July 4 at 8 p.m."
2. Revising an emergency already in effect — extending it ("...now extended through Sunday, \
July 5 at 8 p.m.") or ending it early ("...will now end Friday, July 3 at 11 p.m. instead of \
Saturday"). Treat these the same as a declaration: report the new, final effective window.
3. Announcing that an emergency has ended, e.g. "Heat Health Emergency Ends".

Title: {title}

Content:
{content}

Return ONLY valid JSON with these fields:
- is_active_emergency: true for forms 1 and 2 above (declaring, extending, or shortening an \
emergency that is/was in effect), false for form 3 (announcing the emergency is over).
- starts_at: ISO 8601 datetime (America/New_York, e.g. "2026-07-01T11:00:00") the emergency \
begins or began. If this release doesn't restate the original start time (e.g. a revision that \
only mentions the new end time), return null rather than guessing.
- ends_at: ISO 8601 datetime (America/New_York) — the FINAL, currently-effective end of the \
emergency after applying any extension or early-end described in this release (not the original \
end time if it changed). Null if open-ended or not stated.

This press release was published on {today}. Assume any date without a year is {year}, and \
resolve relative phrases like "today," "tomorrow," or "this afternoon" relative to that \
publish date — NOT relative to your own training/knowledge cutoff.
"""


def parse_heat_emergency(title: str, content: str, reference_date: date | None = None) -> dict:
    """Parse a Philly DPH press release for heat emergency start/end times.

    reference_date should be the press release's own publish date (not today's wall-clock
    date) so relative phrasing in the release resolves correctly regardless of when this
    function happens to run.
    """
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    today = reference_date or date.today()
    prompt = _HEAT_EMERGENCY_PROMPT.format(
        title=title,
        content=content[:8000],
        today=today,
        year=today.year,
    )
    message = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = _response_text(message) or _no_text_note(message)
    try:
        result = _parse_response(raw)
    except (json.JSONDecodeError, ValueError):
        return {"_raw": raw}
    result["_raw"] = raw
    return result


def parse_all_pools(text: str, pool_list: list[dict]) -> list[dict]:
    """Extract schedule info for every pool mentioned in text."""
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    prompt = _ALL_POOLS_PROMPT.format(
        pool_list=_format_pool_list(pool_list),
        content=text[:ALL_POOLS_MAX_CHARS],
    )
    # Haiku, deliberately — unlike parse_all_pools_image below, which is on sonnet.
    # This path reads text that is already text, against a schema with no schedule
    # rules in it, so there is nothing here for a stronger model to be better at:
    # on the phila.gov closings page (the real use case) haiku and sonnet returned
    # the same 62 pools with identical names and dates, haiku in 17.5s to sonnet's
    # 32.2s. The image path has to OCR first, which is where the gap does show.
    # Latency is worth paying for there and not here, since this call blocks an
    # admin request. Re-measure before unifying them.
    with client.messages.stream(
        model="claude-haiku-4-5",
        max_tokens=16000,
        system=_system_prompt(),
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = stream.get_final_message()
    raw = _response_text(message) or _no_text_note(message)
    try:
        return _parse_list_response(raw)
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("parse_all_pools JSON error: %s\nRaw response:\n%s", e, raw)
        raise json.JSONDecodeError(f"{e.msg} — raw response snippet: {raw[:300]!r}", e.doc, e.pos) from e


def parse_all_pools_image(image_bytes: bytes, image_name: str, pool_list: list[dict]) -> list[dict]:
    """Extract schedule info for every pool mentioned in an image."""
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    image_bytes, media_type = _prepare_image_for_claude(image_bytes)
    image_data = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = _ALL_POOLS_IMAGE_PROMPT.format(pool_list=_format_pool_list(pool_list))
    # Sonnet for the OCR; parse_all_pools above is on haiku on purpose — see there.
    with client.messages.stream(
        model="claude-sonnet-5",
        max_tokens=16000,
        system=_system_prompt(),
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": image_data},
                },
                {"type": "text", "text": prompt},
            ],
        }],
    ) as stream:
        message = stream.get_final_message()
    raw = _response_text(message) or _no_text_note(message)
    try:
        return _parse_list_response(raw)
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("parse_all_pools_image JSON error: %s\nRaw response:\n%s", e, raw)
        raise json.JSONDecodeError(f"{e.msg} — raw response snippet: {raw[:300]!r}", e.doc, e.pos) from e


def parse_image_submission(image_bytes: bytes, image_name: str, pool_list: list[dict]) -> dict:
    """Parse an uploaded image (e.g. screenshot) for pool schedule info."""
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    image_bytes, media_type = _prepare_image_for_claude(image_bytes)
    image_data = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = _IMAGE_PROMPT.format(
        pool_list=_format_pool_list(pool_list),
        schedule_instructions=_SCHEDULE_INSTRUCTIONS,
    )
    message = client.messages.create(
        model="claude-sonnet-5",
        # Room for adaptive thinking plus the JSON; thinking tokens count toward
        # max_tokens, and at 1024 the JSON could be truncated mid-object.
        max_tokens=8192,
        system=_system_prompt(),
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_data,
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )
    raw = _response_text(message) or _no_text_note(message)
    try:
        result = _parse_response(raw)
    except (json.JSONDecodeError, ValueError):
        return {"_raw": raw}
    result["_raw"] = raw
    return result
