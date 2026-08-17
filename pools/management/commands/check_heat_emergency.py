from django.core.management.base import BaseCommand

from pools.models import MonitoredPage
from pools.services.page_monitor import check_heat_emergency_page


class Command(BaseCommand):
    help = "Check Philadelphia DPH press releases for heat health emergency declarations."

    def handle(self, *args, **options):
        pages = list(MonitoredPage.objects.filter(page_type="heat_emergency"))
        if not pages:
            self.stderr.write("No heat-emergency monitored pages in database — add one via admin.")
            return
        for page in pages:
            report = check_heat_emergency_page(page)
            for line in report.errors:
                self.stderr.write(line)
            for line in report.highlights:
                self.stdout.write(self.style.SUCCESS(line))
