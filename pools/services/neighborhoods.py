import json
from pathlib import Path

_DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "neighborhoods.json"
_neighborhoods: list[dict] | None = None


def get_neighborhoods() -> list[dict]:
    """Return list of {name, lat, lng} sorted by name."""
    global _neighborhoods
    if _neighborhoods is None:
        with open(_DATA_FILE) as f:
            _neighborhoods = json.load(f)
    return _neighborhoods


def get_neighborhood_centroid(name: str) -> tuple[float, float] | None:
    for n in get_neighborhoods():
        if n["name"] == name:
            return (n["lat"], n["lng"])
    return None
