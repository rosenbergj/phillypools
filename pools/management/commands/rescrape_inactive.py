import requests
from django.core.management.base import BaseCommand
from pools.models import Pool

GEOJSON_URL = (
    "https://hub.arcgis.com/api/v3/datasets/"
    "c6f6176968f04d3f88adbc4c362af55d_0/downloads/data"
    "?format=geojson&spatialRefId=4326&where=1%3D1"
)


class Command(BaseCommand):
    help = "Re-sync only the is_active field from OpenDataPhilly without overwriting manual edits"

    def handle(self, *args, **options):
        self.stdout.write("Fetching pool data from OpenDataPhilly...")
        try:
            response = requests.get(GEOJSON_URL, timeout=30)
            response.raise_for_status()
        except requests.RequestException as e:
            self.stderr.write(f"Failed to fetch data: {e}")
            return

        features = response.json().get("features", [])
        self.stdout.write(f"Found {len(features)} features.")

        scraped = {}
        for feature in features:
            props = feature.get("properties", {})
            amenity_id = str(props.get("ppr_amenity_id") or "")
            if not amenity_id:
                continue
            status_raw = (props.get("pool_status") or "").lower()
            scraped[amenity_id] = "inactive" not in status_raw and "closed" not in status_raw

        updated = skipped = 0
        for pool in Pool.objects.exclude(ppr_amenity_id=""):
            if pool.ppr_amenity_id not in scraped:
                self.stdout.write(self.style.WARNING(f"  Not in feed (unchanged): {pool.name}"))
                skipped += 1
                continue
            new_active = scraped[pool.ppr_amenity_id]
            if pool.is_active != new_active:
                pool.is_active = new_active
                pool.save(update_fields=["is_active"])
                flag = "active" if new_active else "inactive"
                self.stdout.write(f"  Updated → {flag}: {pool.name}")
                updated += 1

        self.stdout.write(self.style.SUCCESS(f"\nDone. Updated: {updated}, Unchanged/skipped: {skipped + (Pool.objects.count() - updated - skipped)}"))
