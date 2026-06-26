from django.core.management.base import BaseCommand
from django.utils import timezone

from pools.models import Pool, PoolSeasonHistory, ScheduleChange, _upsert_season_history


class Command(BaseCommand):
    help = "Clear season-specific pool data at the start of a new season"

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write changes to the database (default is dry run)",
        )
        parser.add_argument(
            "--keep-schedules",
            action="store_true",
            help="Preserve weekday/weekend schedule text instead of archiving and clearing it",
        )

    def handle(self, *args, **options):
        dry_run = not options["apply"]
        clear_schedules = not options["keep_schedules"]
        today = timezone.localdate()

        prefix = "[DRY RUN] " if dry_run else ""

        pools = list(Pool.objects.all())
        self.stdout.write(f"Found {len(pools)} pools.")

        date_fields = [
            "opening_date", "opening_date_source_url",
            "closing_date", "closing_date_source_url",
            "updates", "updates_source_url",
        ]
        if clear_schedules:
            date_fields += [
                "weekday_schedule", "weekday_schedule_source_url",
                "weekend_schedule", "weekend_schedule_source_url",
            ]

        # --- Step 1: Archive date history (also picks up schedule for pools with dates) ---
        history_count = 0
        for pool in pools:
            if pool.opening_date or pool.closing_date:
                history_count += 1
                self.stdout.write(
                    f"  {prefix}Saving history for {pool.name}: "
                    + (f"opens {pool.opening_date}" if pool.opening_date else "")
                    + (" / " if pool.opening_date and pool.closing_date else "")
                    + (f"closes {pool.closing_date}" if pool.closing_date else "")
                    + (" + schedule" if (pool.weekday_schedule or pool.weekend_schedule) else "")
                )
                if not dry_run:
                    _upsert_season_history(pool)
        self.stdout.write(f"\n{prefix}Saved/verified history for {history_count} pool(s).\n")

        # --- Step 2: Archive schedules for pools whose dates were already cleared ---
        # This handles the case where reset_season was run previously (clearing dates)
        # but --keep-schedules was used then. We archive to today.year-1 since we
        # can't determine the season year from the pool's own data any more.
        if clear_schedules:
            fallback_year = today.year - 1
            orphan_count = 0
            for pool in pools:
                if not (pool.weekday_schedule or pool.weekend_schedule):
                    continue
                if pool.opening_date or pool.closing_date:
                    continue  # already handled in step 1
                orphan_count += 1
                self.stdout.write(
                    f"  {prefix}Archiving orphan schedule for {pool.name} → {fallback_year} history"
                )
                if not dry_run:
                    obj, _ = PoolSeasonHistory.objects.get_or_create(pool=pool, year=fallback_year)
                    changed = False
                    # Don't overwrite a history value that was already saved.
                    if pool.weekday_schedule and not obj.weekday_schedule:
                        obj.weekday_schedule = pool.weekday_schedule
                        changed = True
                    if pool.weekend_schedule and not obj.weekend_schedule:
                        obj.weekend_schedule = pool.weekend_schedule
                        changed = True
                    if changed:
                        obj.save()
            if orphan_count:
                self.stdout.write(
                    f"\n{prefix}Archived orphan schedules for {orphan_count} pool(s) "
                    f"(no dates, fell back to year {fallback_year}).\n"
                )

        # --- Step 3: Clear current-season fields ---
        cleared_pools = 0
        for pool in pools:
            had_data = any(getattr(pool, f) for f in date_fields)
            if not had_data:
                continue
            cleared_pools += 1
            self.stdout.write(
                f"  {prefix}Clearing {pool.name}: "
                + ", ".join(f for f in date_fields if getattr(pool, f))
            )

        if not dry_run:
            updates = {f: None if "date" in f else "" for f in date_fields}
            Pool.objects.update(**updates)

        self.stdout.write(f"\n{prefix}Cleared season data on {cleared_pools} pools.")

        # --- Step 4: Delete past ScheduleChanges ---
        past_changes = ScheduleChange.objects.filter(date_to__lt=today)
        past_count = past_changes.count()
        if past_count:
            self.stdout.write(
                f"{prefix}Deleting {past_count} past schedule change(s) "
                f"(date_to before {today})."
            )
            if not dry_run:
                past_changes.delete()

        if dry_run:
            self.stdout.write(
                self.style.WARNING("\nDry run — no changes made. Re-run with --apply to write.")
            )
        else:
            self.stdout.write(self.style.SUCCESS("\nDone."))
