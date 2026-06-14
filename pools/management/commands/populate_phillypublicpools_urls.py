import re
from difflib import get_close_matches

import requests
from bs4 import BeautifulSoup
from django.core.management.base import BaseCommand

from pools.models import Pool

A_TO_Z_URL = (
    "https://phillypublicpools.com/jump-into-the-free-public-pools-in-philly/"
    "philly-public-pools-a-z/"
)

# Words stripped from both sides before comparing, to bridge name differences
# like "Amos Pool" vs "Amos Playground Pool".
_NOISE = re.compile(
    r"\b(pool|playground|recreation|rec|center|park|indoor|outdoor)\b", re.I
)


def _normalize(name: str) -> str:
    name = name.replace("\xa0", " ")
    name = re.sub(r"\(.*?\)", "", name)   # strip parentheticals like "(formerly 39th & Olive)"
    name = _NOISE.sub("", name)
    name = re.sub(r"[^a-z0-9 ]", "", name.lower())
    return re.sub(r"\s+", " ", name).strip()


class Command(BaseCommand):
    help = "Populate phillypublicpools_url for all pools by crawling phillypublicpools.com"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write matches to the database (default is dry-run)",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Overwrite existing phillypublicpools_url values (default: skip)",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        overwrite = options["overwrite"]

        self.stdout.write(f"Fetching {A_TO_Z_URL} ...")
        resp = requests.get(A_TO_Z_URL, timeout=15)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        # Collect unique pool links from the page, preserving first-seen order.
        seen_urls = set()
        external_pools = []  # [(name, url), ...]
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True).replace("\xa0", " ")
            if "/philly-public-pools-a-z/" not in href:
                continue
            if href == A_TO_Z_URL:
                continue
            if "?share=" in href:
                continue
            if href in seen_urls:
                continue
            seen_urls.add(href)
            external_pools.append((text, href))

        self.stdout.write(f"Found {len(external_pools)} pool pages on phillypublicpools.com")

        db_pools = list(Pool.objects.all())
        norm_to_pool = {_normalize(p.name): p for p in db_pools}

        matched = []
        unmatched = []

        for ext_name, ext_url in external_pools:
            norm_ext = _normalize(ext_name)
            # Exact normalized match first.
            if norm_ext in norm_to_pool:
                matched.append((ext_name, ext_url, norm_to_pool[norm_ext]))
                continue
            # Fuzzy fallback.
            close = get_close_matches(norm_ext, norm_to_pool.keys(), n=1, cutoff=0.6)
            if close:
                matched.append((ext_name, ext_url, norm_to_pool[close[0]]))
            else:
                unmatched.append((ext_name, ext_url))

        self.stdout.write("")
        self.stdout.write("=== MATCHES ===")
        updated = 0
        skipped = 0
        for ext_name, ext_url, pool in matched:
            if pool.phillypublicpools_url and not overwrite:
                self.stdout.write(
                    f"  SKIP (already set)  {pool.name!r}  →  {pool.phillypublicpools_url}"
                )
                skipped += 1
                continue
            self.stdout.write(f"  {ext_name!r}  →  {pool.name!r}  →  {ext_url}")
            if apply:
                pool.phillypublicpools_url = ext_url
                pool.save(update_fields=["phillypublicpools_url"])
                updated += 1

        if unmatched:
            self.stdout.write("")
            self.stdout.write("=== NO MATCH FOUND ===")
            for ext_name, ext_url in unmatched:
                self.stdout.write(f"  {ext_name!r}  {ext_url}")

        self.stdout.write("")
        if apply:
            self.stdout.write(
                self.style.SUCCESS(f"Done. Updated {updated}, skipped {skipped} already-set.")
            )
        else:
            self.stdout.write(
                f"Dry run. {len(matched)} matches ({skipped} already set), "
                f"{len(unmatched)} unmatched. Re-run with --apply to write."
            )
