"""Fetching and diffing for MonitoredPage rows.

This lives outside the management commands so a page can also be checked the moment
it's created — from the admin or from the multi-pool apply page — rather than sitting
un-baselined until the next cron run.
"""
import hashlib
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from pools.models import HeatEmergencyPressRelease, HeatHealthEmergency, MonitoredPage, Pool, Submission

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PhillyPools/1.0; +https://phillypools.app)"
}


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


def _fetch(url, report):
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        report.errors.append(f"Fetch error for {url}: {e}")
        return None
    return resp.text


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
    html = _fetch(page.url, report)
    if html is None:
        return report

    content = _extract_content(html)
    new_hash = hashlib.sha256(content.encode()).hexdigest()
    now = timezone.now()

    if not page.content_hash:
        # First run — record baseline without creating a submission
        page.content_hash = new_hash
        page.last_checked = now
        page.save()
        report.notes.append(f"Initialized: {page.url}")
        return report

    page.last_checked = now

    if new_hash == page.content_hash:
        page.save(update_fields=["last_checked"])
        report.notes.append(f"No change: {page.url}")
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
    html = _fetch(page.url, report)
    if html is None:
        return report

    page.last_checked = timezone.now()
    page.save(update_fields=["last_checked"])

    soup = BeautifulSoup(html, "html.parser")
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
