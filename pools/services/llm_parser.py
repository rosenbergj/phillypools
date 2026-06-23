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
For weekday_schedule and weekend_schedule, summarize into compact time blocks, one per line.
Format each line as: "H–H Activity" using an en-dash (e.g. "11–1 Camp Swim" or "1–4 Open Swim").
Use 12-hour times without am/pm unless needed for clarity.
Merge consecutive blocks with the same activity into one span (e.g. "11–12 Day Camp" + "12–1 Day Camp" → "11–1 Day Camp").
Note per-day variations in parentheses after the activity (e.g. "4–5 Swim Lessons (Wed-Thu) / Swim Team (Mon, Tues, Fri)").
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
        result = json.loads(message.content[0].text.strip())
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
Return a JSON array — one object per pool found:
[
  {{
    "pool_id": <integer from the pool list above, or null if no match>,
    "pool_name": "<name as written in the source>",
    "opening_date": "<YYYY-MM-DD or null>",
    "closing_date": "<YYYY-MM-DD or null>",
    "notes": "<any other relevant info excluding address information, or null>"
  }}
]
Return [] if no pool schedule info is found."""

_ALL_POOLS_IMAGE_PROMPT = """Pool list (id: name — address):
{pool_list}

The image above may mention multiple Philadelphia pools. OCR the text and extract ALL pools
mentioned with schedule info. Return a JSON array — one object per pool found:
[
  {{
    "pool_id": <integer from the pool list above, or null if no match>,
    "pool_name": "<name as written in the source>",
    "opening_date": "<YYYY-MM-DD or null>",
    "closing_date": "<YYYY-MM-DD or null>",
    "notes": "<any other relevant info excluding address information, or null>"
  }}
]
Return [] if no pool schedule info is found."""


def _parse_response(response_text: str) -> dict:
    text = response_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
    return json.loads(text)


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
        model="claude-haiku-4-5",
        max_tokens=1024,
        system=_system_prompt(),
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text
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
        content=text[:12000],
    )
    message = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=8192,
        system=_system_prompt(),
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text
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
    message = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=8192,
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
    )
    raw = message.content[0].text
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
        model="claude-haiku-4-5",
        max_tokens=1024,
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
    raw = message.content[0].text
    try:
        result = _parse_response(raw)
    except (json.JSONDecodeError, ValueError):
        return {"_raw": raw}
    result["_raw"] = raw
    return result
