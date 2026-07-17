import bleach
import markdown as markdown_lib
from bs4 import BeautifulSoup
from django.db.models import Q
from django.utils import timezone
from django.utils.safestring import mark_safe

from pools.models import SiteAnnouncement

COLOR_STYLES = {
    "cyan": "background-color: #cff4fc; color: #055160; border-color: #9eeaf9;",
    "yellow": "background-color: #fff3cd; color: #664d03; border-color: #ffe69c;",
    "orange": "background-color: #fd7e14; color: #fff; border-color: #e07214;",
    "red": "background-color: #dc3545; color: #fff; border-color: #bd2130;",
}

# Display order when multiple announcements are active at once: most urgent first.
COLOR_PRIORITY = {"red": 0, "orange": 1, "yellow": 2, "cyan": 3}

ALLOWED_TAGS = ["a", "strong", "em", "b", "i", "code", "br"]
ALLOWED_ATTRIBUTES = {"a": ["href"]}


def render_message(message: str):
    """Render an announcement's markdown message to sanitized HTML, safe to output unescaped.

    Markdown's own <p> wrapper is dropped (not in ALLOWED_TAGS) since the banner is
    a single inline line, not a block of prose.
    """
    html = markdown_lib.markdown(message)
    cleaned = bleach.clean(html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRIBUTES, strip=True)
    soup = BeautifulSoup(cleaned, "html.parser")
    for link in soup.find_all("a"):
        link["target"] = "_blank"
        link["rel"] = "noopener"
        link["style"] = "color: inherit; text-decoration: underline;"
    return mark_safe(str(soup))


def get_active_announcements() -> list[SiteAnnouncement]:
    """Return all site announcements currently within their start/end window, red first."""
    now = timezone.now()
    announcements = list(SiteAnnouncement.objects.filter(
        starts_at__lte=now
    ).filter(
        Q(ends_at__isnull=True) | Q(ends_at__gte=now)
    ))
    announcements.sort(key=lambda a: COLOR_PRIORITY[a.color])
    return announcements
