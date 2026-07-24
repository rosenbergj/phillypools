"""
Delete all accumulated usage stats — raw events and the permanent daily
aggregates alike — so /stats/ starts fresh from now.

Unlike the offseason archive step (which keeps UsageDaily for year-over-year),
this throws the daily totals away too. Use it for a deliberate clean slate, e.g.
after a measurement change makes the old numbers not worth comparing against.

Dry-run by default: it only reports what it would delete. Pass --yes to delete.
VisitorSalt is left untouched so today's in-flight visitor counting stays
consistent — today's raw rows are gone, but a fresh salt would needlessly split
any visitor seen both before and after the reset.
"""
from django.core.management.base import BaseCommand

from pools.models import UsageDaily, UsageEvent, UsageRollupState


class Command(BaseCommand):
    help = "Delete all usage stats (raw events and daily aggregates) for a fresh start."

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true",
            help="Actually delete. Without this the command only reports what it would remove.",
        )

    def handle(self, *args, **options):
        models = [UsageEvent, UsageDaily, UsageRollupState]
        for model in models:
            self.stdout.write(f"{model.__name__}: {model.objects.count()} rows")

        if not options["yes"]:
            self.stdout.write(self.style.WARNING("Dry run — pass --yes to delete."))
            return

        for model in models:
            model.objects.all().delete()
        self.stdout.write(self.style.SUCCESS("Deleted all usage stats. /stats/ now starts from now."))
