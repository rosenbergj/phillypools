import re

import requests
from bs4 import BeautifulSoup
from django.core.management.base import BaseCommand

from pools.models import Pool

# 2024 first (most pools); 2026 fills gaps. Earlier year takes priority.
SCHEDULE_URLS = [
    "https://www.phila.gov/2024-06-14-philadelphia-2024-public-pool-opening-schedule/",
    "https://www.phila.gov/2026-06-12-philadelphia-2026-public-pool-opening-schedule/",
]


_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; phillypools-admin/1.0)"}


def _parse_schedule_page(url) -> dict[str, str]:
    """Return {pool_name_as_written: address} parsed from a phila.gov schedule page."""
    resp = requests.get(url, timeout=30, headers=_HEADERS)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    result = {}
    for li in soup.find_all("li"):
        a = li.find("a")
        if not a:
            continue
        name = a.get_text(strip=True)
        text = li.get_text(separator=" ")
        # Address follows an en-dash (–), em-dash (—), or hyphen after the name
        m = re.search(r'[–—-]\s*(.+?)\.?\s*$', text)
        if m:
            result[name] = m.group(1).strip().rstrip(".")
    return result


def _normalize(name: str) -> str:
    """Lowercase, remove 'pool', collapse whitespace and punctuation."""
    name = name.lower()
    name = re.sub(r"\bpool\b", "", name)
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    return " ".join(name.split())


def _word_overlap(s1: str, s2: str) -> float:
    """Fraction of the smaller meaningful-word-set that appears in the larger.
    Single-character tokens are excluded — they're too ambiguous (e.g. 'M', 'J')."""
    w1 = {w for w in s1.split() if len(w) > 1}
    w2 = {w for w in s2.split() if len(w) > 1}
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / min(len(w1), len(w2))


def _build_matches(
    ppr_addresses: dict[str, str],
    db_pools: dict[str, "Pool"],
    min_score: float,
) -> tuple[list, list]:
    """
    Greedily assign PPR schedule entries to DB pools (1:1).
    Returns (matched, unmatched_ppr) where matched is a list of
    (ppr_name, ppr_address, pool, score) sorted by descending score.
    """
    # Score every (ppr_name, db_pool) pair
    candidates = []
    for ppr_name, ppr_address in ppr_addresses.items():
        norm = _normalize(ppr_name)
        if norm in db_pools:
            candidates.append((1.0, ppr_name, ppr_address, db_pools[norm]))
        else:
            for norm_db, pool in db_pools.items():
                score = _word_overlap(norm, norm_db)
                if score >= min_score:
                    candidates.append((score, ppr_name, ppr_address, pool))

    # Greedy 1:1 assignment: best score first
    candidates.sort(key=lambda x: -x[0])
    assigned_ppr, assigned_db = set(), set()
    matched = []
    for score, ppr_name, ppr_address, pool in candidates:
        if ppr_name in assigned_ppr or pool.pk in assigned_db:
            continue
        matched.append((ppr_name, ppr_address, pool, score))
        assigned_ppr.add(ppr_name)
        assigned_db.add(pool.pk)

    unmatched_ppr = [(n, a) for n, a in ppr_addresses.items() if n not in assigned_ppr]
    return matched, unmatched_ppr


class Command(BaseCommand):
    help = "Update pool addresses from phila.gov PPR schedule pages (better than OpenDataPhilly)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Write changes to the database (default is dry run)")
        parser.add_argument("--min-score", type=float, default=0.5,
                            help="Minimum word-overlap score to accept a fuzzy name match (default: 0.5)")

    def handle(self, *args, **options):
        dry_run = not options["apply"]
        min_score = options["min_score"]

        # Fetch and merge schedule pages; first URL wins on conflicts
        ppr_addresses: dict[str, str] = {}
        for url in SCHEDULE_URLS:
            self.stdout.write(f"Fetching {url} ...")
            try:
                page_data = _parse_schedule_page(url)
                for name, addr in page_data.items():
                    if name not in ppr_addresses:
                        ppr_addresses[name] = addr
            except Exception as e:
                self.stderr.write(f"  Failed: {e}")
        self.stdout.write(f"Found {len(ppr_addresses)} pool entries across schedule pages.\n")

        # Build normalized name → Pool lookup, including alternate names
        db_pools = {}
        for p in Pool.objects.prefetch_related("alternate_names").all():
            db_pools[_normalize(p.name)] = p
            for alt in p.alternate_names.all():
                db_pools[_normalize(alt.name)] = p

        matched, unmatched_ppr = _build_matches(ppr_addresses, db_pools, min_score)

        # Report matches
        changed = [(ppr_name, ppr_address, pool, score)
                   for ppr_name, ppr_address, pool, score in matched
                   if pool.address != ppr_address]
        unchanged = [(ppr_name, ppr_address, pool, score)
                     for ppr_name, ppr_address, pool, score in matched
                     if pool.address == ppr_address]

        if unchanged:
            self.stdout.write(f"  {len(unchanged)} pool(s) already have the correct address — skipping.")

        if changed:
            self.stdout.write(f"\n{'[DRY RUN] ' if dry_run else ''}Address updates ({len(changed)}):")
            for ppr_name, ppr_address, pool, score in changed:
                confidence = "exact" if score == 1.0 else f"fuzzy {score:.0%}"
                self.stdout.write(
                    f"  {pool.name} [{confidence}]\n"
                    f"    {pool.address!r}  →  {ppr_address!r}"
                )
                if not dry_run:
                    pool.address = ppr_address
                    pool.save(update_fields=["address"])

        if unmatched_ppr:
            self.stdout.write(self.style.WARNING(
                f"\nCould not match {len(unmatched_ppr)} PPR schedule entr{'y' if len(unmatched_ppr) == 1 else 'ies'} to a pool in the DB:"
            ))
            for ppr_name, ppr_address in unmatched_ppr:
                self.stdout.write(f"  {ppr_name!r} — {ppr_address}")

        # Report DB pools with no PPR address found
        matched_db_ids = {pool.pk for _, _, pool, _ in matched}
        unmatched_db = Pool.objects.exclude(pk__in=matched_db_ids)
        if unmatched_db.exists():
            self.stdout.write(self.style.WARNING(
                f"\n{unmatched_db.count()} DB pool(s) not found in any schedule page (address unchanged):"
            ))
            for pool in unmatched_db:
                self.stdout.write(f"  {pool.name}")

        if dry_run:
            self.stdout.write(self.style.WARNING("\nDry run — re-run with --apply to write."))
        else:
            self.stdout.write(self.style.SUCCESS(f"\nUpdated {len(changed)} pool address(es)."))
