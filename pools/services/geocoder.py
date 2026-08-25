import requests

from pools.services.user_agents import GEOCODER as USER_AGENT

_cache: dict[str, tuple[float, float] | None] = {}
_polygon_cache: dict[str, dict | None] = {}

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


def geocode_zip(zip_code: str) -> tuple[float, float] | None:
    zip_code = zip_code.strip()
    if zip_code in _cache:
        return _cache[zip_code]

    try:
        response = requests.get(
            NOMINATIM_URL,
            params={
                "q": zip_code,
                "countrycodes": "us",
                "format": "json",
                "limit": 1,
                "polygon_geojson": 1,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=5,
        )
        response.raise_for_status()
        results = response.json()
        if results:
            r = results[0]
            _cache[zip_code] = (float(r["lat"]), float(r["lon"]))
            _polygon_cache[zip_code] = r.get("geojson")
            return _cache[zip_code]
    except Exception:
        pass

    _cache[zip_code] = None
    _polygon_cache[zip_code] = None
    return None


def get_zip_polygon(zip_code: str) -> dict | None:
    zip_code = zip_code.strip()
    if zip_code not in _polygon_cache:
        geocode_zip(zip_code)
    return _polygon_cache.get(zip_code)
