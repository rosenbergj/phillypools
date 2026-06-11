from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, ProgrammingError
from pools.models import Submission


class Command(BaseCommand):
    help = "Abort deployment if pending submissions with uploaded images exist"

    def handle(self, *args, **options):
        try:
            blocking = Submission.objects.filter(
                status="pending",
                uploaded_image__isnull=False,
            ).exclude(uploaded_image="")

            if blocking.exists():
                ids = list(blocking.values_list("id", flat=True))
                raise CommandError(
                    f"DEPLOY BLOCKED: {len(ids)} pending submission(s) with uploaded images "
                    f"(IDs: {ids}). Review and clear them in /admin/pools/submission/ "
                    f"before redeploying, or images will be lost."
                )

            self.stdout.write(self.style.SUCCESS("No blocking submissions. Deploy OK."))
        except (OperationalError, ProgrammingError):
            # DB unavailable or not yet migrated (fresh deploy) — nothing to block on.
            self.stdout.write(self.style.WARNING("DB not reachable during build — skipping check."))
