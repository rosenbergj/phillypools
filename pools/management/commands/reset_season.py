from django.core.management.base import BaseCommand
from django.utils import timezone

from pools.models import Pool, PoolSeasonHistory, ScheduleChange, _upsert_season_history


class Command(BaseCommand):
    help = "Clear season-specific pool data at the start of a new season"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be cleared without making changes",
        )
        parser.add_argument(
            "--clear-schedules",
            action="store_true",
            help="Also clear weekday/weekend schedule text and source URLs",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        clear_schedules = options["clear_schedules"]
        today = timezone.localdate()

        prefix = "[DRY RUN] " if dry_run else ""

        pools = Pool.objects.all()
        self.stdout.write(f"Found {pools.count()} pools.")

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

        # Persist season history before clearing (idempotent — uses update_or_create)
        history_count = 0
        for pool in pools:
            if pool.opening_date or pool.closing_date:
                history_count += 1
                self.stdout.write(
                    f"  {prefix}Saving history for {pool.name}: "
                    + (f"opens {pool.opening_date}" if pool.opening_date else "")
                    + (" / " if pool.opening_date and pool.closing_date else "")
                    + (f"closes {pool.closing_date}" if pool.closing_date else "")
                )
                if not dry_run:
                    _upsert_season_history(pool)
        self.stdout.write(f"\n{prefix}Saved/verified history for {history_count} pool(s).\n")

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

        # Past ScheduleChanges: filter to those whose date_to is before today
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
                self.style.WARNING("\nDry run — no changes made. Re-run without --dry-run to apply.")
            )
        else:
            self.stdout.write(self.style.SUCCESS("\nDone."))
