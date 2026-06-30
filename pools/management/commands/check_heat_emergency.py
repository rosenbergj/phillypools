import requests
from bs4 import BeautifulSoup
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from pools.models import HeatEmergencyPressRelease, HeatHealthEmergency

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PhillyPools/1.0; +https://phillypools.app)"
}
_DPH_URL = "https://www.phila.gov/departments/department-of-public-health/"


def _parse_alert_datetime(value):
    if not value:
        return None
    dt = parse_datetime(value)
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


class Command(BaseCommand):
    help = "Check Philadelphia DPH press releases for heat health emergency declarations."

    def handle(self, *args, **options):
        try:
            resp = requests.get(_DPH_URL, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            self.stderr.write(f"Fetch error for {_DPH_URL}: {e}")
            return

        soup = BeautifulSoup(resp.text, "html.parser")
        grid = soup.find("div", class_="press-grid")
        if not grid:
            self.stderr.write("press-grid not found — page structure may have changed")
            return

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

            self.stdout.write(self.style.SUCCESS(f"New heat emergency release: {title}"))
            self._create_press_release(url, title, published_at)

    def _create_press_release(self, url, title, published_at):
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

        HeatEmergencyPressRelease.objects.create(
            title=title,
            source_url=url,
            raw_content=raw_content,
            published_at=published_at,
            is_active_emergency=parsed.get("is_active_emergency", True),
            parsed_starts_at=_parse_alert_datetime(parsed.get("starts_at")),
            parsed_ends_at=_parse_alert_datetime(parsed.get("ends_at")),
            llm_response=llm_response,
            emergency=suggested_emergency,
        )
