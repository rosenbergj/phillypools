import requests
from datetime import date
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

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write changes to the database (default is dry run)')

    def handle(self, *args, **options):
        dry_run = not options['apply']
        prefix = "[DRY RUN] " if dry_run else ""

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
        seen_amenity_ids = set()

        for feature in features:
            props = feature.get("properties", {})
            geometry = feature.get("geometry", {})

            amenity_id = str(props.get("ppr_amenity_id") or "")
            if not amenity_id:
                self.stderr.write(f"Skipping feature with no ppr_amenity_id: {props.get('pool_name')}")
                skipped += 1
                continue

            seen_amenity_ids.add(amenity_id)

            name = (
                props.get("official_name")
                or props.get("pool_name")
                or props.get("site_name")
                or "Unknown"
            ).strip()

            address_parts = [props.get("address_911") or "", props.get("zip_code") or ""]
            address = ", ".join(p for p in address_parts if p).strip()

            coords = geometry.get("coordinates") if geometry else None
            lat = round(coords[1], 5) if coords and len(coords) >= 2 else None
            lng = round(coords[0], 5) if coords and len(coords) >= 2 else None

            pool_type_raw = (props.get("pool_type") or "").strip()
            pool_type = POOL_TYPE_MAP.get(pool_type_raw, "outdoor")

            status_raw = (props.get("pool_status") or "").lower()
            is_active = "inactive" not in status_raw and "closed" not in status_raw

            city_comment = (props.get("comments") or "").strip()

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
            }

            existing = Pool.objects.filter(ppr_amenity_id=amenity_id).first()
            if dry_run:
                if existing:
                    updated += 1
                    self.stdout.write(f"  {prefix}Would update: {name}")
                else:
                    created += 1
                    self.stdout.write(f"  {prefix}Would create: {name}")
            else:
                pool, was_created = Pool.objects.update_or_create(
                    ppr_amenity_id=amenity_id,
                    defaults=defaults,
                )
                if was_created:
                    pool.notes = city_comment
                    pool.save(update_fields=["notes"])
                    created += 1
                    self.stdout.write(f"  Created: {name}")
                else:
                    if city_comment and city_comment not in (pool.notes or ""):
                        today_str = date.today().strftime("%m/%d/%Y")
                        pool.notes = (pool.notes + "\n" if pool.notes else "") + f"{today_str} update: {city_comment}"
                        pool.save(update_fields=["notes"])
                    updated += 1
                    self.stdout.write(f"  Updated: {name}")

        label = "Would create" if dry_run else "Created"
        label2 = "would update" if dry_run else "updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{prefix}{label}: {created}, {label2}: {updated}, skipped: {skipped}"
            )
        )

        # Warn about pools in the DB that are no longer in the feed
        stale = Pool.objects.exclude(ppr_amenity_id="").exclude(ppr_amenity_id__in=seen_amenity_ids)
        if stale.exists():
            self.stdout.write(self.style.WARNING(
                "\nPools in DB not found in current feed — review manually:"
            ))
            for p in stale:
                self.stdout.write(f"  {p.name} (id={p.pk}, ppr_amenity_id={p.ppr_amenity_id})")
        else:
            self.stdout.write("All DB pools accounted for in feed.")

        if dry_run:
            self.stdout.write(self.style.WARNING('Dry run — re-run with --apply to write.'))
        else:
            self.stdout.write("Assigning neighborhoods...")
            from django.core.management import call_command
            call_command("assign_neighborhoods")
