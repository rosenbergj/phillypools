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
* Both are read for narrow derived facts and then dropped: a browser family and
  major version from the user-agent, and from the address a single yes/no for
  whether it belongs to a hosting provider (see services/datacenter.py). Neither
  the address nor the string reaches the database in any form.
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
    # Security and inventory scanners, SEO crawlers, AI agents, and link-preview
    # fetchers seen identifying themselves in the live logs but slipping past the
    # list above. "networkingextension" is Apple's preview fetcher, which loads the
    # og: image when someone shares the link in Messages — a share is worth knowing
    # about, but the fetch is not a visit.
    "scan", "xpanse", "censys", "shodan", "zgrab", "masscan", "nuclei", "nmap",
    "netcraft", "read-aloud", "networkingextension", "ahrefs", "semrush",
    "dataprovider", "archive.org", "site24x7", "statuscake",
    "openai", "anthropic", "perplexity",
    # Catches "GoogleOther" and Google's other non-Googlebot fetchers, which carry
    # no "bot"/"crawl"/"spider" token of their own. No real browser's UA contains
    # the word "google".
    "google",
]

# Ways a user-agent can contradict itself. A string that no shipped browser would
# ever send is a spoof attempt, and a spoof attempt is a bot however plausible the
# rest of it reads — these catch clients that copy a browser's UA but get a detail
# wrong, which no substring list ever will.
_CHROMIUM_SUFFIX = re.compile(r"safari/537\.36\b")


def _forged_ua(ua: str) -> bool:
    """True if `ua` (already lowercased) is not a string any real browser sends."""
    # A URL where the product token belongs: vulnerability scanners announcing the
    # payload they are probing for.
    if ua.startswith("http://") or ua.startswith("https://"):
        return True
    # Every Chromium build, on every platform, ends its WebKit claim at Safari/537.36.
    # Anything else claiming Chrome typed the string by hand.
    if "chrome/" in ua and "safari/" in ua and not _CHROMIUM_SUFFIX.search(ua):
        return True
    # Gecko and WebKit are different engines; no build reports both products.
    if "firefox/" in ua and "applewebkit" in ua:
        return True
    # A bare "Safari" with no version, which the real one always carries.
    if "safari" in ua and "safari/" not in ua:
        return True
    return False


# Fragments of paths this site has never served, and that no link anywhere points
# at: PHP, WordPress, and the usual leaked-secret filenames. Only ever tested
# against a request that already 404'd, so a real URL can never match one, and
# deliberately narrow — a plausible guess at a page we might have had, like
# /contact, is somebody looking for something, not somebody rattling doorknobs.
_PROBE_PATTERNS = [
    ".php", ".asp", ".jsp", ".cgi", "/wp-", "/wordpress", "/xmlrpc",
    "/.env", "/.git", "/.aws", "/.ssh", "/vendor/", "/cgi-bin/",
    "/phpmyadmin", "/administrator", "/adminer", "/telescope", "/actuator",
    "/config.json", "/credentials", "/backup",
]


# Headers a client sets when it is fetching a page on the chance it will be needed,
# rather than because anyone asked for it: Chrome and the standard use Sec-Purpose,
# older Chrome sent Purpose, Firefox sends X-Moz and Safari X-Purpose. Google's
# prefetch proxy carries these too, which is what makes it recognisable at all —
# the user-agent it forwards is the real browser's and gives nothing away.
_SPECULATIVE_HEADERS = [
    ("HTTP_SEC_PURPOSE", ("prefetch", "prerender")),
    ("HTTP_PURPOSE", ("prefetch",)),
    ("HTTP_X_MOZ", ("prefetch", "prerender")),
    ("HTTP_X_PURPOSE", ("preview", "prefetch")),
]


def is_speculative(request) -> bool:
    """
    True if this fetch is a guess about what someone might do next, not something
    they did.

    Nobody has seen the page at this point and may never; if they do go on to open
    it, the beacon fires from their own browser and counts them properly then. So
    these are dropped rather than filed as robots: the fetch may well come from the
    visitor's own machine, and calling it a robot would discredit the real visit
    they are about to make.
    """
    for header, needles in _SPECULATIVE_HEADERS:
        value = request.META.get(header, "").lower()
        if any(n in value for n in needles):
            return True
    return False


def is_probe_path(path: str) -> bool:
    """True if a 404 for `path` is a scanner working through a list, not a bad link."""
    path = (path or "").lower()
    return any(p in path for p in _PROBE_PATTERNS)


_MOBILE_PATTERNS = ["mobi", "android", "iphone", "ipad", "ipod", "windows phone"]

# Browser families, in the order they must be tested. Two orderings matter: iOS
# builds go first, since every browser on iOS is WebKit underneath and signs off
# with the same Safari token as Safari itself; and Edge, Opera and Samsung all
# carry a Chrome token too, so Chrome has to be tried last of the Chromium four.
_CHROME_VERSION = re.compile(r"chrome/(\d+)")
_UA_FAMILIES = [
    ("chrome-ios", re.compile(r"crios/(\d+)")),
    ("firefox-ios", re.compile(r"fxios/(\d+)")),
    ("edge-ios", re.compile(r"edgios/(\d+)")),
    ("edge", re.compile(r"edga?/(\d+)")),
    ("opera", re.compile(r"opr/(\d+)")),
    ("samsung", re.compile(r"samsungbrowser/(\d+)")),
    ("chrome", _CHROME_VERSION),
    ("firefox", re.compile(r"firefox/(\d+)")),
    ("safari", re.compile(r"version/(\d+)[\d.]*\s+(?:mobile/\S+\s+)?safari/")),
]


def ua_family(user_agent: str) -> str:
    """
    A low-cardinality description of what the user-agent claims to be, like
    "chrome/129" or "safari/13" — never the string itself.

    Purely descriptive: it records the claim, while `classify_client` records the
    verdict, so a breakdown can show that traffic calling itself a current Chrome
    was still classified as a crawler. The major version is worth the handful of
    extra values it costs, because a frozen version is the clearest tell a scraper
    leaves — a fleet announcing iOS 13 years after the fact stands out at a glance,
    where a bare "safari" would hide in with everyone's phone. Browser and major
    version alone carry far too little entropy to identify anyone.

    Anything unrecognised, including every self-identified bot, folds into "other":
    naming individual crawlers is a different question than this column answers,
    and letting their version strings in here would blow the cardinality open.
    """
    ua = (user_agent or "").lower()
    if not ua:
        return ""
    for family, pattern in _UA_FAMILIES:
        match = pattern.search(ua)
        if match:
            return f"{family}/{match.group(1)}"
    return "other"

# Endpoints only ever requested by the site's own JavaScript. They are not linked
# from any page and not in the sitemap, so a hit here is proof that a real browser
# rendered and ran the page — a JS check that costs no extra request. Used by the
# rollup to distinguish confirmed browsers from merely not-obviously-a-bot traffic.
# "pageview_js" is the page-load beacon: it confirms passive readers who never
# filter or click a pin, who otherwise leave no JS trace at all.
JS_ONLY_EVENTS = {"filter", "map_pick", "pin_click", "card_click", "pageview_js"}

# Requests for a rendered HTML page, as opposed to the JSON and beacon endpoints
# the page calls afterwards. Counted distinct-by-(event, key), so reloading one
# page is still one page but index -> a pool detail is two.
PAGE_EVENTS = {"index", "pool_view", "submit_view", "submit_done", "other"}

# Doing something with the page rather than only reading it: opening a pool's
# popup from either the map or the list, picking a neighborhood off the map, and
# the zip/status/neighborhood filters (which redraw the markers, so they are map
# use too). "pageview_js" is deliberately absent — it fires on its own and proves
# only that a browser loaded.
INTERACTION_EVENTS = {"filter", "map_pick", "pin_click", "card_click"}

# Who a stored breakdown counts. Every ranked breakdown is rolled up once for each,
# so /stats/ can switch between them long after the raw rows are gone. "human" is
# everyone the crawler check did not catch; "confirmed" is the subset whose browser
# ran the page's JavaScript, and is what /stats/ shows unless asked otherwise —
# traffic that never ran a line of it is more likely an uncaught crawler than a
# person browsing with JavaScript off.
AUDIENCE_HUMAN = "human"
AUDIENCE_CONFIRMED = "confirmed"
AUDIENCES = (AUDIENCE_CONFIRMED, AUDIENCE_HUMAN)

# How a confirmed browser spent their day. Ordered most to least engaged, which is
# the order /stats/ shows them in. The passive, single-page case is split by which
# page it was: list and detail are the two pages a real visit is ever just one of,
# so "other" (the submit form, its confirmation, or no page event at all — just the
# beacon) is expected to sit at zero and exists only so a stray case isn't silently
# folded into one of the other two.
JOURNEY_MULTI_PAGE = "multi_page"
JOURNEY_SINGLE_ENGAGED = "single_engaged"
JOURNEY_SINGLE_PASSIVE_LIST = "single_passive_list"
JOURNEY_SINGLE_PASSIVE_DETAIL = "single_passive_detail"
JOURNEY_SINGLE_PASSIVE_OTHER = "single_passive_other"


def classify_journey(events) -> str:
    """
    Bucket one visitor's day from the set of event names (with keys) they produced.

    `events` is an iterable of (event, key) pairs. A visitor with no page event at
    all — possible if the beacon lands but the page request itself wasn't recorded
    — counts as single-page: they are certainly not known to have seen two.
    """
    pages = {(e, k) for e, k in events if e in PAGE_EVENTS}
    if len(pages) > 1:
        return JOURNEY_MULTI_PAGE
    if any(e in INTERACTION_EVENTS for e, _ in events):
        return JOURNEY_SINGLE_ENGAGED
    if len(pages) == 1:
        (event, _key), = pages
        if event == "index":
            return JOURNEY_SINGLE_PASSIVE_LIST
        if event == "pool_view":
            return JOURNEY_SINGLE_PASSIVE_DETAIL
    return JOURNEY_SINGLE_PASSIVE_OTHER

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
    if any(p in ua for p in _BOT_PATTERNS) or _forged_ua(ua):
        return "bot"
    return "unknown"


def forged_browser_headers(request) -> bool:
    """
    True if the request's headers contradict the browser its user-agent claims to be.

    A scraper can copy a user-agent string in one line; sending everything else a
    real browser sends takes real effort, so the headers around the claim are a far
    better lie detector than the claim itself. Checked only on page navigations:
    the site's own fetch() calls send a different and narrower header set, and a
    client that reached one of those endpoints has already proved it runs the page.

    Nothing here is recorded — the headers are read for a yes/no and dropped.
    """
    ua = request.META.get("HTTP_USER_AGENT", "").lower()
    if not ua or ua_family(ua) in ("", "other"):
        return False  # Not claiming to be a browser, so there is nothing to contradict.

    # Every Chromium since 90 sends client hints, but only over HTTPS — so this can
    # only be asked of a secure request, and never of local development over HTTP.
    if request.is_secure() and "chrome/" in ua and not request.META.get("HTTP_SEC_CH_UA"):
        match = _CHROME_VERSION.search(ua)
        if match and int(match.group(1)) >= 90:
            return True

    # A browser asks for a language and says it wants HTML. A script asks for
    # anything and doesn't care what it gets.
    if not request.META.get("HTTP_ACCEPT_LANGUAGE"):
        return True
    accept = request.META.get("HTTP_ACCEPT", "")
    if not accept or accept.strip() == "*/*":
        return True
    return False


def classify_request(request, navigation: bool = False) -> str:
    """
    As classify_client, but recognises the site's own staff first, and on page
    navigations also cross-checks the user-agent against the rest of the headers.

    Staff traffic stays inside the visitor totals — the admin browsing the live
    site is still a real person using it — but is labelled so its share can be
    seen and, if it ever grows enough to distort a number, subtracted after the
    fact from data already collected.
    """
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated and user.is_staff:
        return "staff"
    verdict = classify_client(request.META.get("HTTP_USER_AGENT", ""))
    if verdict == "unknown" and navigation and forged_browser_headers(request):
        return "bot"
    return verdict


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
