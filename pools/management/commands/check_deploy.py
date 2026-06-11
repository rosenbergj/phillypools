from django.core.management.base import BaseCommand, CommandError
from pools.models import Submission


class Command(BaseCommand):
    help = "Abort deployment if pending submissions with uploaded images exist"

    def handle(self, *args, **options):
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
