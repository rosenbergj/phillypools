import io
import traceback

from django.core.management import call_command
from django.core.management.base import BaseCommand

from pools.services.digest import send_digest_if_needed

CHECK_COMMANDS = ["check_pool_schedule", "check_heat_emergency"]


class Command(BaseCommand):
    help = (
        "Run all url-watcher checks, collecting scraper errors, then send the "
        "pending-items digest email if warranted."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the digest email instead of sending it; don't update digest state.",
        )

    def handle(self, *args, **options):
        errors = []
        for name in CHECK_COMMANDS:
            stderr = io.StringIO()
            try:
                call_command(name, stdout=self.stdout, stderr=stderr)
            except Exception:
                errors.append(f"{name} crashed: {traceback.format_exc(limit=3)}")
            captured = stderr.getvalue().strip()
            if captured:
                self.stderr.write(captured)
                errors.extend(f"{name}: {line}" for line in captured.splitlines())

        result = send_digest_if_needed(
            scrape_errors=errors, dry_run=options["dry_run"], out=self.stdout.write
        )
        self.stdout.write(f"Digest: {result}")
