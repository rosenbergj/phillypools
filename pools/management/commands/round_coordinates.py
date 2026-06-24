from django.core.management.base import BaseCommand

from pools.models import Pool


class Command(BaseCommand):
    help = "Round all pool latitude/longitude values to 5 decimal places"

    def handle(self, *args, **options):
        updated = 0
        for pool in Pool.objects.exclude(latitude=None, longitude=None):
            new_lat = round(pool.latitude, 5) if pool.latitude is not None else None
            new_lng = round(pool.longitude, 5) if pool.longitude is not None else None
            if new_lat != pool.latitude or new_lng != pool.longitude:
                pool.latitude = new_lat
                pool.longitude = new_lng
                pool.save(update_fields=["latitude", "longitude"])
                updated += 1
        self.stdout.write(f"Updated {updated} pool(s).")
