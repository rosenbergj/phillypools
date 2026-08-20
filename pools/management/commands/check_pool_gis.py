from django.core.management.base import BaseCommand

from pools.services.gis import check_pool_gis


class Command(BaseCommand):
    help = (
        "Check the city's ArcGIS pool layer for date changes and create submissions "
        "for review when it disagrees with what a pool currently holds."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be proposed without creating submissions or saving state.",
        )

    def handle(self, *args, **options):
        report = check_pool_gis(dry_run=options["dry_run"])
        for line in report.errors:
            self.stderr.write(line)
        for line in report.highlights:
            self.stdout.write(self.style.SUCCESS(line))
        for line in report.notes:
            self.stdout.write(line)
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run — no submissions created, no state saved."))
