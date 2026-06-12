import base64
import json
import mimetypes
import os
from datetime import date
from pathlib import Path

import anthropic


def _system_prompt() -> str:
    today = date.today()
    return (
        "You are a data extraction assistant for Philadelphia public swimming pools.\n"
        "Extract pool schedule information from the provided content and return ONLY valid JSON.\n"
        "Use null for fields you cannot determine. Dates must be YYYY-MM-DD format.\n"
        "Pool IDs come from the provided list; set pool_id to null if unsure.\n"
        f"Today's date is {today}. If a date in the content has no year, assume it is {today.year}.\n"
        f"Set stale_year_warning to true if the content appears to be from a prior season "
        f"(e.g. references {today.year - 1} or earlier dates as current)."
    )

_PROMPT_TEMPLATE = """Pool list (id: name):
{pool_list}

Content to analyze:
{content}

Return JSON with exactly these fields:
{{
  "pool_id": <integer or null>,
  "opening_date": "<YYYY-MM-DD or null>",
  "closing_date": "<YYYY-MM-DD or null>",
  "weekday_schedule": "<detailed weekday periods, one per line, or null>",
  "weekend_schedule": "<detailed weekend periods, one per line, or null>",
  "notes": "<any other relevant info or null>",
  "confidence": "<high|medium|low>",
  "stale_year_warning": <true|false>
}}"""

_IMAGE_PROMPT = """Pool list (id: name):
{pool_list}

The image above is a screenshot (e.g. from Instagram or a website) with Philadelphia pool schedule information.
OCR the text in the image and extract pool schedule data.

Return JSON with exactly these fields:
{{
  "pool_id": <integer or null>,
  "opening_date": "<YYYY-MM-DD or null>",
  "closing_date": "<YYYY-MM-DD or null>",
  "weekday_schedule": "<detailed weekday periods, one per line, or null>",
  "weekend_schedule": "<detailed weekend periods, one per line, or null>",
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

    media_type, _ = mimetypes.guess_type(image_name)
    if not media_type or not media_type.startswith("image/"):
        media_type = "image/jpeg"

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


def _format_pool_list(pool_list: list[dict]) -> str:
    return "\n".join(f"{p['id']}: {p['name']}" for p in pool_list)


_ALL_POOLS_PROMPT = """Pool list (id: name):
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
    "notes": "<any other relevant info or null>"
  }}
]
Return [] if no pool schedule info is found."""

_ALL_POOLS_IMAGE_PROMPT = """Pool list (id: name):
{pool_list}

The image above may mention multiple Philadelphia pools. OCR the text and extract ALL pools
mentioned with schedule info. Return a JSON array — one object per pool found:
[
  {{
    "pool_id": <integer from the pool list above, or null if no match>,
    "pool_name": "<name as written in the source>",
    "opening_date": "<YYYY-MM-DD or null>",
    "closing_date": "<YYYY-MM-DD or null>",
    "notes": "<any other relevant info or null>"
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
    result = json.loads(text)
    return result if isinstance(result, list) else []


def parse_submission(text: str, pool_list: list[dict]) -> dict:
    """Parse text content (fetched from a URL) for pool schedule info."""
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    prompt = _PROMPT_TEMPLATE.format(
        pool_list=_format_pool_list(pool_list),
        content=text[:8000],
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
        max_tokens=4096,
        system=_system_prompt(),
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_list_response(message.content[0].text)


def parse_all_pools_image(image_bytes: bytes, image_name: str, pool_list: list[dict]) -> list[dict]:
    """Extract schedule info for every pool mentioned in an image."""
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    media_type, _ = mimetypes.guess_type(image_name)
    if not media_type or not media_type.startswith("image/"):
        media_type = "image/jpeg"

    image_data = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = _ALL_POOLS_IMAGE_PROMPT.format(pool_list=_format_pool_list(pool_list))
    message = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=4096,
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
    return _parse_list_response(message.content[0].text)


def parse_image_submission(image_bytes: bytes, image_name: str, pool_list: list[dict]) -> dict:
    """Parse an uploaded image (e.g. screenshot) for pool schedule info."""
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    media_type, _ = mimetypes.guess_type(image_name)
    if not media_type or not media_type.startswith("image/"):
        media_type = "image/jpeg"

    image_data = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = _IMAGE_PROMPT.format(pool_list=_format_pool_list(pool_list))
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
