from django.core.management.base import BaseCommand

from pools.models import MonitoredPage
from pools.services.page_monitor import check_pool_info_page


class Command(BaseCommand):
    help = "Check monitored pages for content changes and create submissions when they change."

    def handle(self, *args, **options):
        pages = list(MonitoredPage.objects.filter(page_type="pool_info"))
        if not pages:
            self.stderr.write("No pool-info monitored pages in database — add one via admin.")
            return
        for page in pages:
            report = check_pool_info_page(page)
            for line in report.errors:
                self.stderr.write(line)
            for line in report.highlights:
                self.stdout.write(self.style.SUCCESS(line))
            for line in report.notes:
                self.stdout.write(line)
