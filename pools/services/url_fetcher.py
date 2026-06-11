import re

import requests
from bs4 import BeautifulSoup

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PhillyPools/1.0; +https://phillypools.app)"
}


def fetch_url(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL and return its stripped text content."""
    resp = requests.get(url, headers=_HEADERS, timeout=10)
    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    if "html" in content_type:
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
    else:
        text = resp.text

    # Collapse whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()[:max_chars]
