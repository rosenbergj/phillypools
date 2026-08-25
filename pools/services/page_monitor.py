"""Fetching and diffing for MonitoredPage rows.

This lives outside the management commands so a page can also be checked the moment
it's created — from the admin or from the multi-pool apply page — rather than sitting
un-baselined until the next cron run.
"""
import hashlib
from dataclasses import dataclass, field
from datetime import timedelta

import requests
from bs4 import BeautifulSoup
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from pools.models import HeatEmergencyPressRelease, HeatHealthEmergency, MonitoredPage, Pool, Submission
from pools.services.user_agents import CRAWLER_HEADERS


@dataclass
class CheckReport:
    """What one check of one page produced. `notes` are routine ("no change"),
    `highlights` are worth calling out (something was detected), and `errors` are
    written to stderr by the commands, which feeds them into the digest email."""
    notes: list[str] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self):
        return "; ".join(self.errors + self.highlights + self.notes)


# Bump whenever the way we read a page body changes — `_extract_content`, or the
# press-release parsing below. Conditional requests mean an unchanged page is never
# re-read, so without this a parser fix would silently never run against the pages
# already baselined: the page is identical, but what we'd get out of it is not.
EXTRACTOR_VERSION = 1

# How stale the last real body may get before we stop sending validators and demand
# one. This is the floor on how long a lying ETag — or a CDN edge answering 304 from
# a stale copy — could hide a change from us. 23 rather than 24 hours so the same
# daily cron slot always trips it instead of the forced fetch drifting later each day.
FULL_FETCH_MAX_AGE = timedelta(hours=23)


@dataclass
class Fetched:
    """One attempt at a page. Exactly one of these is true: `failed`, `not_modified`,
    or `html` holds a body."""
    html: str | None = None
    not_modified: bool = False
    failed: bool = False
    etag: str = ""
    last_modified: str = ""


def _validators_for(page, now):
    """Headers that let the server answer 304 — or nothing, when we want a real body.

    phila.gov hands us a *weak* validator (`W/"..."`), because `requests` asks for
    gzip and the compressed representation isn't byte-identical to the raw one.
    Weak is the right strength for this job: it promises the content is equivalent,
    not that the octets match, and equivalence is exactly the question we're asking.
    It does mean the guarantee rests on the server's notion of "the same page",
    which is what FULL_FETCH_MAX_AGE below is the backstop for.

    We deliberately ask for the full page when we've never stored a validator, when
    the parser has moved on since this hash was computed, or when the last body we
    actually saw is older than FULL_FETCH_MAX_AGE.
    """
    if page.extractor_version != EXTRACTOR_VERSION:
        return {}
    if page.last_full_fetch is None or now - page.last_full_fetch > FULL_FETCH_MAX_AGE:
        return {}
    headers = {}
    if page.etag:
        headers["If-None-Match"] = page.etag
    if page.last_modified:
        headers["If-Modified-Since"] = page.last_modified
    return headers


def _fetch(url, report, validators=None):
    try:
        resp = requests.get(
            url, headers={**CRAWLER_HEADERS, **(validators or {})}, timeout=15
        )
        # Checked before raise_for_status, which is indifferent to a 3xx: this is a
        # successful answer meaning "your copy is current", not an error.
        if resp.status_code == 304:
            return Fetched(not_modified=True)
        resp.raise_for_status()
    except Exception as e:
        report.errors.append(f"Fetch error for {url}: {e}")
        return Fetched(failed=True)
    return Fetched(
        html=resp.text,
        etag=resp.headers.get("ETag", ""),
        last_modified=resp.headers.get("Last-Modified", ""),
    )


def _record_body(page, fetched, now):
    """Remember what the server said about the body we just read. Only ever called on
    a 200 — a 304 must not advance `last_full_fetch`, or the forced refetch above
    would never come due and the floor would be no floor at all."""
    page.etag = fetched.etag
    page.last_modified = fetched.last_modified
    page.last_full_fetch = now
    page.extractor_version = EXTRACTOR_VERSION


def check_page(page):
    """Check one page, dispatching on its type. Never raises — problems come back
    as `errors` on the report."""
    if page.page_type == "heat_emergency":
        return check_heat_emergency_page(page)
    return check_pool_info_page(page)


# --- pool info pages -------------------------------------------------------

def _extract_content(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    block = soup.find("div", class_="post-content")
    if block:
        return block.get_text(separator="\n", strip=True)
    # Fallback: body minus chrome
    for tag in soup(["nav", "header", "footer", "script", "style"]):
        tag.decompose()
    return (soup.find("body") or soup).get_text(separator="\n", strip=True)


def check_pool_info_page(page):
    report = CheckReport()
    now = timezone.now()
    fetched = _fetch(page.url, report, _validators_for(page, now))
    if fetched.failed:
        return report

    page.last_checked = now

    if fetched.not_modified:
        # The server's validator covers the whole body, which is a superset of the
        # text we hash — so "byte-identical body" implies "identical extract", and a
        # 304 can't be hiding a change. Nothing else on the row moves.
        page.save(update_fields=["last_checked"])
        report.notes.append(f"Not modified (304): {page.url}")
        return report

    _record_body(page, fetched, now)
    content = _extract_content(fetched.html)
    new_hash = hashlib.sha256(content.encode()).hexdigest()

    if not page.content_hash:
        # First run — record baseline without creating a submission
        page.content_hash = new_hash
        page.save()
        report.notes.append(f"Initialized: {page.url}")
        return report

    if new_hash == page.content_hash:
        page.save()
        # Said differently from the 304 line above on purpose: this one means we
        # read a real body and found it identical, which is what a page whose chrome
        # moves — but whose content didn't — looks like.
        report.notes.append(f"No change (full fetch): {page.url}")
        return report

    page.content_hash = new_hash
    page.last_changed = now
    page.save()
    report.highlights.append(f"Changed: {page.url} — creating submission")
    _create_submission(page.url)
    return report


def _create_submission(url):
    from pools.services.llm_parser import parse_submission
    from pools.services.url_fetcher import fetch_url

    pool_list = list(Pool.objects.all().values("id", "name"))
    raw_content = ""
    llm_response = None
    parsed_fields = {}

    try:
        raw_content = fetch_url(url)
    except Exception:
        pass

    try:
        parsed_fields = parse_submission(raw_content, pool_list)
        llm_response = parsed_fields.pop("_raw", None)
    except Exception as e:
        llm_response = {"error": str(e)}

    parsed_pool = None
    if parsed_fields.get("pool_id"):
        try:
            parsed_pool = Pool.objects.get(pk=parsed_fields["pool_id"])
        except Pool.DoesNotExist:
            pass

    parsed_notes = parsed_fields.get("notes") or ""
    if parsed_fields.get("stale_year_warning"):
        parsed_notes = (
            "WARNING: Source may be from a prior season — verify dates before applying.\n"
            + parsed_notes
        )

    Submission.objects.create(
        url=url,
        submitter_note="Auto-detected: page content changed",
        raw_fetched_content=raw_content,
        llm_response=llm_response,
        parsed_pool=parsed_pool,
        parsed_opening_date=parsed_fields.get("opening_date"),
        parsed_closing_date=parsed_fields.get("closing_date"),
        parsed_weekday_schedule=parsed_fields.get("weekday_schedule") or "",
        parsed_weekend_schedule=parsed_fields.get("weekend_schedule") or "",
        parsed_notes=parsed_notes,
        llm_confidence=parsed_fields.get("confidence") or "",
    )


# --- heat emergency pages --------------------------------------------------

def _parse_alert_datetime(value):
    if not value:
        return None
    dt = parse_datetime(value)
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def check_heat_emergency_page(page):
    report = CheckReport()
    now = timezone.now()
    fetched = _fetch(page.url, report, _validators_for(page, now))
    if fetched.failed:
        return report

    page.last_checked = now

    if fetched.not_modified:
        # An unchanged listing page can't have gained a press release.
        page.save(update_fields=["last_checked"])
        report.notes.append(f"Not modified (304): {page.url}")
        return report

    _record_body(page, fetched, now)
    page.save()

    soup = BeautifulSoup(fetched.html, "html.parser")
    grid = soup.find("div", class_="press-grid")
    if not grid:
        report.errors.append(f"press-grid not found on {page.url} — page structure may have changed")
        return report

    existing_urls = set(HeatEmergencyPressRelease.objects.values_list("source_url", flat=True))

    # Page lists newest-first; process oldest-first so that when multiple new releases
    # land in one run (e.g. a same-day revision), they're created — and reviewed — in
    # true chronological order.
    articles = list(reversed(grid.find_all("article", class_="type-press_release")))
    for article in articles:
        link = article.find("a", class_="card--press_release")
        if not link or not link.get("href"):
            continue
        url = link["href"].strip()

        title_tag = link.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else ""
        if "heat health emergency" not in title.lower():
            continue
        if url in existing_urls:
            continue

        time_tag = link.find("time")
        published_at = parse_date(time_tag["datetime"]) if time_tag and time_tag.get("datetime") else None

        report.highlights.append(f"New heat emergency release: {title}")
        _create_press_release(url, title, published_at)

    if not report.highlights:
        report.notes.append(f"No new press releases: {page.url}")
    return report


def _create_press_release(url, title, published_at):
    from pools.services.llm_parser import parse_heat_emergency
    from pools.services.url_fetcher import fetch_url

    raw_content = ""
    try:
        raw_content = fetch_url(url)
    except Exception:
        pass

    parsed = {}
    llm_response = None
    try:
        parsed = parse_heat_emergency(title, raw_content, reference_date=published_at)
        llm_response = parsed
    except Exception as e:
        llm_response = {"error": str(e)}

    # A "Declares" release always starts something new; anything else (an extension or an
    # "ends" notice) is presumed to act on whichever emergency is still open. The admin can
    # always correct this before applying.
    suggested_emergency = None
    if "declares" not in title.lower():
        suggested_emergency = (
            HeatHealthEmergency.objects.filter(ends_at__isnull=True).order_by("-starts_at").first()
            or HeatHealthEmergency.objects.order_by("-starts_at").first()
        )

    release_kind = "ends" if parsed.get("is_active_emergency") is False else "declares_or_extends"

    HeatEmergencyPressRelease.objects.create(
        title=title,
        source_url=url,
        raw_content=raw_content,
        published_at=published_at,
        release_kind=release_kind,
        parsed_starts_at=_parse_alert_datetime(parsed.get("starts_at")),
        parsed_ends_at=_parse_alert_datetime(parsed.get("ends_at")),
        llm_response=llm_response,
        emergency=suggested_emergency,
    )


# --- creation --------------------------------------------------------------

def start_monitoring(url, page_type="pool_info"):
    """Create (or find) a monitored page and check it right away, so a pool-info page
    gets its baseline hash immediately instead of on the next cron run.

    Returns (page, created, report) — report is None if the page already existed."""
    page, created = MonitoredPage.objects.get_or_create(url=url, defaults={"page_type": page_type})
    if not created:
        return page, False, None
    return page, True, check_page(page)
