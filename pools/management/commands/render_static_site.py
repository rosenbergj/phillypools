"""Render a static snapshot of the site for the offseason.

Why this exists: the Railway project is torn down between seasons (see
`offseason-runbook.md`), and the naive stand-in — redirecting every
/pools/<slug>/ URL to a single "see you in the spring" page — costs the site its
search presence. A months-long 302 gets treated as permanent, and pointing ~70
distinct URLs at one unrelated page is the textbook soft-404 pattern, so the pool
pages fall out of the index and have to re-earn their rankings every May. Serving
real archived content at the same URLs keeps them indexed and answers the
question a March searcher is actually asking.
"""

import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string
from django.utils import timezone

from pools.models import Pool
from pools.services.favicon import ico_from_png
from pools.views import nearby_pools_context

DEFAULT_BASE_URL = "https://phillypools.app"


def _format_duration(opening_date, closing_date):
    """'7 weeks, 3 days' for a completed season, or None if it can't be computed."""
    if not (opening_date and closing_date):
        return None
    total = (closing_date - opening_date).days + 1
    if total <= 0:
        return None
    weeks, days = divmod(total, 7)
    parts = []
    if weeks:
        parts.append(f"{weeks} week{'s' if weeks != 1 else ''}")
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    return ", ".join(parts)


def season_snapshot(pool, season_year):
    """Everything the static page needs about `pool`'s `season_year`.

    Reads PoolSeasonHistory first, since that's the durable record, but falls
    back to the pool's live fields. The fallback is load-bearing in two cases:

      * `_upsert_season_history` only creates a history row when the pool has an
        opening or closing date (models.py), so a pool that got schedule text but
        never got dates has no row at all — without the fallback its page would
        render with no hours.
      * This command runs before `reset_season` (which stays at the *start* of
        the next season), so the live fields are still populated and are the
        freshest copy of the schedule.
    """
    history = pool.season_history.filter(year=season_year).first()

    opening_date = history.opening_date if history else None
    closing_date = history.closing_date if history else None
    weekday_schedule = (history.weekday_schedule if history else "") or ""
    weekend_schedule = (history.weekend_schedule if history else "") or ""

    # Only trust the live dates if they belong to the season being rendered; a
    # stale prior-season date would otherwise be relabeled as this season's.
    if not opening_date and pool.opening_date and pool.opening_date.year == season_year:
        opening_date = pool.opening_date
    if not closing_date and pool.closing_date and pool.closing_date.year == season_year:
        closing_date = pool.closing_date

    if not weekday_schedule:
        weekday_schedule = pool.weekday_schedule or ""
    if not weekend_schedule:
        weekend_schedule = pool.weekend_schedule or ""

    return {
        "opening_date": opening_date,
        "closing_date": closing_date,
        "weekday_schedule": weekday_schedule,
        "weekend_schedule": weekend_schedule,
        "duration": _format_duration(opening_date, closing_date),
    }


class Command(BaseCommand):
    help = (
        "Render a static offseason snapshot of the site (pool pages, index, "
        "sitemap, robots.txt) into a directory ready to deploy to Cloudflare Pages."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--season-year",
            type=int,
            default=None,
            help=(
                "The season being archived. Defaults to the current calendar year, "
                "which is right for a shutdown run in the fall — pass it explicitly "
                "if you're rendering after January 1."
            ),
        )
        parser.add_argument(
            "--out",
            default=None,
            help="Output directory (default: offseason-build/ at the repo root).",
        )
        parser.add_argument(
            "--assets",
            default=None,
            help=(
                "Directory of hand-maintained static assets copied verbatim into the "
                "output (default: offseason/ at the repo root)."
            ),
        )
        parser.add_argument(
            "--base-url",
            default=DEFAULT_BASE_URL,
            help=f"Absolute site root for canonical URLs and the sitemap (default: {DEFAULT_BASE_URL}).",
        )

    def _say(self, message):
        if self.verbosity:
            self.stdout.write(message)

    def handle(self, *args, **options):
        self.verbosity = options["verbosity"]
        today = timezone.localdate()
        season_year = options["season_year"] or today.year
        next_year = season_year + 1
        base_url = options["base_url"].rstrip("/")
        out = Path(options["out"] or Path(settings.BASE_DIR) / "offseason-build")
        assets = Path(options["assets"] or Path(settings.BASE_DIR) / "offseason")

        if not assets.is_dir():
            raise CommandError(f"Assets directory not found: {assets}")

        # Every pool, inactive included: is_active only means we don't expect an opening
        # date, and the page still carries an address, notes and last season's history.
        # Matches PoolSitemap, so the indexed URL set doesn't churn at the cutover.
        pools = list(
            Pool.objects.prefetch_related("season_history").order_by("name")
        )
        if not pools:
            raise CommandError(
                "No pools found — refusing to render an empty site. "
                "Check that this is pointed at the production database."
            )

        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)

        # Hand-maintained assets first (pic.png, favicon.png, og-preview.png,
        # _redirects); generated files below may intentionally overwrite them.
        copied = 0
        for item in sorted(assets.iterdir()):
            if item.is_dir():
                shutil.copytree(item, out / item.name)
            else:
                shutil.copy2(item, out / item.name)
            copied += 1
        self._say(f"Copied {copied} asset(s) from {assets}")

        # The live site answers /favicon.ico from a Django view, which isn't here
        # to do it once the site is static — and Google's favicon crawler goes to
        # that path. Built from the favicon.png just copied above, so the icon
        # search results show survives the offseason unchanged.
        favicon_png = out / "favicon.png"
        if favicon_png.exists():
            (out / "favicon.ico").write_bytes(ico_from_png(favicon_png.read_bytes()))
            self._say("Rendered favicon.ico")
        else:
            # Not fatal — a custom --assets dir is free to leave it out — but loud,
            # because a real deploy missing it loses the icon in search results.
            self.stderr.write(
                f"No favicon.png in {assets}: skipping favicon.ico, and "
                "<link rel=icon> will point at a file that isn't there."
            )

        shared = {
            "base_url": base_url,
            "season_year": season_year,
            "next_year": next_year,
        }

        pools_without_schedule = []
        for pool in pools:
            season = season_snapshot(pool, season_year)
            if not (season["weekday_schedule"] or season["weekend_schedule"]):
                pools_without_schedule.append(pool.name)
            where = f" in {pool.neighborhood}" if pool.neighborhood else ""
            # Past tense, because this is written after the season is over. Guarded on
            # the season's opening date rather than is_active alone: a pool that opened
            # and was deactivated afterwards did open, so its season should read
            # normally. Nothing here speculates about next season — is_active describes
            # the season just ended, and we have no information about the next one.
            did_not_open = not pool.is_active and not season["opening_date"]
            page_dir = out / "pools" / pool.slug
            page_dir.mkdir(parents=True, exist_ok=True)
            html = render_to_string(
                "pools/offseason_detail.html",
                {
                    **shared,
                    "pool": pool,
                    "season": season,
                    "did_not_open": did_not_open,
                    # Offseason: neighbours regardless of status, since nothing is open
                    # and an inactive pool is still a place someone may want to know about.
                    **nearby_pools_context(pool, pools, today, offseason=True),
                    "page_title": f"{pool.name} — {season_year} Schedule — Philly Pools",
                    "meta_description": (
                        f"{pool.name}, a Philadelphia public pool{where}. "
                        f"Did not open in {season_year}."
                        if did_not_open else
                        f"{pool.name}, a Philadelphia public pool{where}. Hours and "
                        f"schedule from the {season_year} season; {next_year} dates "
                        f"to be announced."
                    ),
                    "canonical_path": pool.get_absolute_url(),
                },
            )
            (page_dir / "index.html").write_text(html, encoding="utf-8")
        self._say(f"Rendered {len(pools)} pool page(s)")

        index_description = (
            f"Philly Pools tracks hours and schedules for {len(pools)} Philadelphia "
            f"public pools. The {season_year} season has ended — browse "
            f"{season_year} schedules while we wait for {next_year} dates."
        )
        pages = (
            (
                "index.html",
                "pools/offseason_index.html",
                {
                    "page_title": "Philly Pools — Philadelphia Public Pool Schedules",
                    "meta_description": index_description,
                    "canonical_path": "/",
                },
            ),
            (
                "404.html",
                "pools/offseason_404.html",
                {
                    "page_title": "Page not found — Philly Pools",
                    "meta_description": index_description,
                    "canonical_path": "/",
                    "robots_meta": "noindex, follow",
                },
            ),
            ("robots.txt", "pools/offseason_robots.txt", {}),
            ("sitemap.xml", "pools/offseason_sitemap.xml", {}),
        )
        for name, template, extra in pages:
            content = render_to_string(
                template,
                {
                    **shared,
                    "pools": pools,
                    "rendered_on": today,
                    **extra,
                },
            )
            (out / name).write_text(content, encoding="utf-8")
            self._say(f"Rendered {name}")

        # Not a warning: PPR simply doesn't publish hours for many pools, and roughly
        # half the inventory having no schedule is the normal, expected state. Reported
        # as a plain count so it reads as coverage information rather than a problem to
        # go fix; pass -v 2 for the names.
        if pools_without_schedule:
            self._say(
                f"\n{len(pools_without_schedule)} of {len(pools)} pool(s) have no "
                f"{season_year} schedule and use the generic-hours copy."
            )
            if self.verbosity >= 2:
                for name in pools_without_schedule:
                    self._say(f"  {name}")

        self._say(
            self.style.SUCCESS(
                f"\nDone. {season_year} snapshot written to {out}\n"
                f"Preview with:  python -m http.server -d {out}\n"
                f"Deploy with:   wrangler pages deploy {out}"
            )
        )
