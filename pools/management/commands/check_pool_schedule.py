import hashlib

import requests
from bs4 import BeautifulSoup
from django.core.management.base import BaseCommand
from django.utils import timezone

from pools.models import MonitoredPage, Pool, Submission

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PhillyPools/1.0; +https://phillypools.app)"
}


def _extract_content(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    block = soup.find("div", class_="post-content")
    if block:
        return block.get_text(separator="\n", strip=True)
    # Fallback: body minus chrome
    for tag in soup(["nav", "header", "footer", "script", "style"]):
        tag.decompose()
    return (soup.find("body") or soup).get_text(separator="\n", strip=True)


class Command(BaseCommand):
    help = "Check monitored pages for content changes and create submissions when they change."

    def handle(self, *args, **options):
        pages = list(MonitoredPage.objects.filter(page_type="pool_info"))
        if not pages:
            self.stderr.write("No pool-info monitored pages in database — add one via admin.")
            return
        for page in pages:
            self._check(page)

    def _check(self, page):
        try:
            resp = requests.get(page.url, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            self.stderr.write(f"Fetch error for {page.url}: {e}")
            return

        content = _extract_content(resp.text)
        new_hash = hashlib.sha256(content.encode()).hexdigest()
        now = timezone.now()

        if not page.content_hash:
            # First run — record baseline without creating a submission
            page.content_hash = new_hash
            page.last_checked = now
            page.save()
            self.stdout.write(f"Initialized: {page.url}")
            return

        page.last_checked = now

        if new_hash == page.content_hash:
            page.save(update_fields=["last_checked"])
            self.stdout.write(f"No change: {page.url}")
            return

        page.content_hash = new_hash
        page.last_changed = now
        page.save()
        self.stdout.write(self.style.SUCCESS(f"Changed: {page.url} — creating submission"))
        self._create_submission(page.url)

    def _create_submission(self, url):
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
