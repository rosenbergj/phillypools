import base64
import json
import mimetypes
import os
from pathlib import Path

import anthropic

_SYSTEM = """You are a data extraction assistant for Philadelphia public swimming pools.
Extract pool schedule information from the provided content and return ONLY valid JSON.
Use null for fields you cannot determine. Dates must be YYYY-MM-DD format.
Pool IDs come from the provided list; set pool_id to null if unsure."""

_PROMPT_TEMPLATE = """Pool list (id: name):
{pool_list}

Content to analyze:
{content}

Return JSON with exactly these fields:
{{
  "pool_id": <integer or null>,
  "opening_date": "<YYYY-MM-DD or null>",
  "closing_date": "<YYYY-MM-DD or null>",
  "hours": "<short summary like '11am-7pm daily' or null>",
  "weekday_schedule": "<detailed weekday periods, one per line, or null>",
  "weekend_schedule": "<detailed weekend periods, one per line, or null>",
  "notes": "<any other relevant info or null>",
  "confidence": "<high|medium|low>"
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
  "hours": "<short summary like '11am-7pm daily' or null>",
  "weekday_schedule": "<detailed weekday periods, one per line, or null>",
  "weekend_schedule": "<detailed weekend periods, one per line, or null>",
  "notes": "<any other relevant info or null>",
  "confidence": "<high|medium|low>"
}}"""


def _format_pool_list(pool_list: list[dict]) -> str:
    return "\n".join(f"{p['id']}: {p['name']}" for p in pool_list)


def _parse_response(response_text: str) -> dict:
    text = response_text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
    return json.loads(text)


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
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text
    result = _parse_response(raw)
    result["_raw"] = raw
    return result


def parse_image_submission(image_path: str, pool_list: list[dict]) -> dict:
    """Parse an uploaded image (e.g. screenshot) for pool schedule info."""
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    path = Path(image_path)
    media_type, _ = mimetypes.guess_type(str(path))
    if not media_type or not media_type.startswith("image/"):
        media_type = "image/jpeg"

    with open(path, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")

    prompt = _IMAGE_PROMPT.format(pool_list=_format_pool_list(pool_list))
    message = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        system=_SYSTEM,
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
    result = _parse_response(raw)
    result["_raw"] = raw
    return result
