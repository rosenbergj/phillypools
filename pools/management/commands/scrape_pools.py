import requests
from django.core.management.base import BaseCommand
from pools.models import Pool

GEOJSON_URL = (
    "https://hub.arcgis.com/api/v3/datasets/"
    "c6f6176968f04d3f88adbc4c362af55d_0/downloads/data"
    "?format=geojson&spatialRefId=4326&where=1%3D1"
)

# Map dataset pool_type values to our model's choices. Anything not
# listed here falls back to "outdoor".
POOL_TYPE_MAP = {
    "Outdoor": "outdoor",
    "Indoor": "indoor",
    "Wading": "wading",
    "Spray": "spray",
}


class Command(BaseCommand):
    help = "Import Philadelphia public pools from the OpenDataPhilly GeoJSON feed"

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

        created = updated = skipped = 0

        for feature in features:
            props = feature.get("properties", {})
            geometry = feature.get("geometry", {})

            amenity_id = str(props.get("ppr_amenity_id") or "")
            if not amenity_id:
                self.stderr.write(f"Skipping feature with no ppr_amenity_id: {props.get('pool_name')}")
                skipped += 1
                continue

            name = (
                props.get("official_name")
                or props.get("pool_name")
                or props.get("site_name")
                or "Unknown"
            ).strip()

            address_parts = [props.get("address_911") or "", props.get("zip_code") or ""]
            address = ", ".join(p for p in address_parts if p).strip()

            coords = geometry.get("coordinates") if geometry else None
            lat = coords[1] if coords and len(coords) >= 2 else None
            lng = coords[0] if coords and len(coords) >= 2 else None

            pool_type_raw = (props.get("pool_type") or "").strip()
            pool_type = POOL_TYPE_MAP.get(pool_type_raw, "outdoor")

            status_raw = (props.get("pool_status") or "").lower()
            is_active = "active" in status_raw

            notes = (props.get("comments") or "").strip()

            neighborhood = (
                props.get("ppr_ops_district")
                or props.get("ppr_prog_district")
                or ""
            ).strip()

            defaults = {
                "name": name,
                "address": address,
                "latitude": lat,
                "longitude": lng,
                "neighborhood": neighborhood,
                "pool_type": pool_type,
                "is_active": is_active,
                "notes": notes,
            }

            pool, was_created = Pool.objects.update_or_create(
                ppr_amenity_id=amenity_id,
                defaults=defaults,
            )

            if was_created:
                created += 1
                self.stdout.write(f"  Created: {name}")
            else:
                updated += 1
                self.stdout.write(f"  Updated: {name}")

        self.stdout.write(
            self.style.SUCCESS(
                f"\nDone. Created: {created}, Updated: {updated}, Skipped: {skipped}"
            )
        )
