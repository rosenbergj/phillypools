from django.core.management.base import BaseCommand

from pools.services.digest import send_digest_if_needed


class Command(BaseCommand):
    help = (
        "Send the pending-items digest email if warranted, without running any "
        "checks first. Standalone entry point for manual runs or a future "
        "notification-only cron."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the digest email instead of sending it; don't update digest state.",
        )

    def handle(self, *args, **options):
        result = send_digest_if_needed(dry_run=options["dry_run"], out=self.stdout.write)
        self.stdout.write(f"Digest: {result}")
