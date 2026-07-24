"""
Cookieless, first-party usage measurement.

Design constraints (see the discussion in offseason-runbook.md / README for why):

* No new cookies, and no third-party analytics beacon. Everything recorded here
  is a request the site already makes in order to function, plus one explicit
  map-pin event.
* No raw IP addresses or user-agent strings are ever stored. A visitor is
  identified only by `visitor_hash()`, a truncated hash of IP + user-agent mixed
  with a random salt that rotates at local midnight and is never retained past
  the day it belongs to. Same person on the same day hashes the same (so daily
  uniques and per-visitor journeys work); the next day they are unlinkable, and
  once the salt is gone the hash cannot be worked backwards even by us.
* Raw rows are pruned after USAGE_RAW_RETENTION_DAYS; permanent history lives in
  the aggregated UsageDaily table, which contains counts only.
"""
import hashlib
import re
import secrets
import threading
from urllib.parse import urlsplit

from django.utils import timezone

# How long individual UsageEvent rows are kept before `rollup_usage` prunes them.
# Long enough to re-run bot classification over a mistake; short enough to stay tidy.
USAGE_RAW_RETENTION_DAYS = 30

# Substrings that identify a non-human client. Matched case-insensitively against
# the user-agent. Deliberately broad: a human misfiled as a bot shows up as a
# missing visitor, which is far less misleading than a crawler counted as a person.
_BOT_PATTERNS = [
    "bot", "crawl", "spider", "slurp", "search", "scrap", "fetch", "monitor",
    "curl", "wget", "python-requests", "http-client", "httpx", "okhttp", "java/",
    "go-http", "libwww", "lighthouse", "headless", "phantomjs", "preview",
    "facebookexternalhit", "embedly", "quora link", "whatsapp", "telegram",
    "slackbot", "discord", "skypeuripreview", "pingdom", "uptime", "railway",
]

_MOBILE_PATTERNS = ["mobi", "android", "iphone", "ipad", "ipod", "windows phone"]

# Endpoints only ever requested by the site's own JavaScript. They are not linked
# from any page and not in the sitemap, so a hit here is proof that a real browser
# rendered and ran the page — a JS check that costs no extra request. Used by the
# rollup to distinguish confirmed browsers from merely not-obviously-a-bot traffic.
# "pageview_js" is the page-load beacon: it confirms passive readers who never
# filter or click a pin, who otherwise leave no JS trace at all.
JS_ONLY_EVENTS = {"filter", "map_pick", "pin_click", "pageview_js"}

_SITE_HOSTS = {"phillypools.app", "www.phillypools.app", "localhost", "127.0.0.1"}

_salt_lock = threading.Lock()
_salt_cache = {"day": None, "salt": None}


def get_client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def _todays_salt(day):
    """
    Return today's random salt, creating it on first use.

    Cached per process so the common path costs no query, but the authoritative
    copy lives in the database: gunicorn runs multiple workers, and if each held
    its own in-memory salt the same visitor would hash differently depending on
    which worker answered, inflating unique counts. Yesterday's row is deleted as
    a side effect, so only the current day's salt is ever on disk.
    """
    cached = _salt_cache
    if cached["day"] == day:
        return cached["salt"]

    from pools.models import VisitorSalt

    with _salt_lock:
        if _salt_cache["day"] == day:
            return _salt_cache["salt"]
        row, _ = VisitorSalt.objects.get_or_create(
            day=day, defaults={"salt": secrets.token_hex(16)}
        )
        VisitorSalt.objects.exclude(day=day).delete()
        _salt_cache["day"] = day
        _salt_cache["salt"] = row.salt
        return row.salt


def visitor_hash(request, day=None) -> str:
    """A per-visitor, per-day pseudonym. Not reversible once the day's salt rotates."""
    day = day or timezone.localdate()
    raw = "|".join([
        _todays_salt(day),
        get_client_ip(request),
        request.META.get("HTTP_USER_AGENT", ""),
        day.isoformat(),
    ])
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def classify_client(user_agent: str) -> str:
    """
    'bot' for clients that identify themselves as automated, else 'unknown'.

    There is deliberately no 'human' verdict here: nothing about a single request
    can prove a person made it. The rollup promotes a visitor to confirmed-browser
    status when it sees a JS_ONLY_EVENTS hit from them.
    """
    if not user_agent:
        return "bot"
    ua = user_agent.lower()
    return "bot" if any(p in ua for p in _BOT_PATTERNS) else "unknown"


def classify_request(request) -> str:
    """
    As classify_client, but recognises the site's own staff first.

    Staff traffic stays inside the visitor totals — the admin browsing the live
    site is still a real person using it — but is labelled so its share can be
    seen and, if it ever grows enough to distort a number, subtracted after the
    fact from data already collected.
    """
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated and user.is_staff:
        return "staff"
    return classify_client(request.META.get("HTTP_USER_AGENT", ""))


def classify_device(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if not ua:
        return ""
    return "mobile" if any(p in ua for p in _MOBILE_PATTERNS) else "desktop"


def referrer_host(referer: str) -> str:
    """
    Host only — never the full referring URL, which can carry search terms and
    other personal detail. Internal navigation returns '' so it doesn't drown out
    the external sources, which are the interesting part.
    """
    if not referer:
        return ""
    host = (urlsplit(referer).hostname or "").lower()
    if host in _SITE_HOSTS:
        return ""
    # Fold the www. variant in, or the same source lands in two ranked rows.
    if host.startswith("www."):
        host = host[4:]
    if not host or host in _SITE_HOSTS:
        return ""
    return host[:100]


_ZIP_RE = re.compile(r"^\d{5}$")


def clean_zip(value: str) -> str:
    value = (value or "").strip()
    return value if _ZIP_RE.match(value) else ""
