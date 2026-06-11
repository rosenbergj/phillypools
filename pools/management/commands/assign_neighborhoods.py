import json
from pathlib import Path

from django.core.management.base import BaseCommand

from pools.models import Pool

DATA = Path(__file__).resolve().parents[2] / "data" / "neighborhoods.json"


def _point_in_ring(lon, lat, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def find_neighborhood(lat, lng, hoods):
    for hood in hoods:
        geom = hood["geometry"]
        polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
        for rings in polys:
            if _point_in_ring(lng, lat, rings[0]):
                return hood["name"]
    return ""


class Command(BaseCommand):
    help = "Assign neighborhood names to pools via point-in-polygon against OpenDataPhilly boundaries"

    def handle(self, *args, **options):
        with open(DATA) as f:
            hoods = json.load(f)

        updated = unmatched = 0
        for pool in Pool.objects.filter(latitude__isnull=False):
            name = find_neighborhood(pool.latitude, pool.longitude, hoods)
            pool.neighborhood = name
            pool.save(update_fields=["neighborhood"])
            if name:
                updated += 1
            else:
                unmatched += 1
                self.stdout.write(self.style.WARNING(f"  No neighborhood found: {pool.name}"))

        self.stdout.write(self.style.SUCCESS(f"Done: {updated} assigned, {unmatched} unmatched"))
