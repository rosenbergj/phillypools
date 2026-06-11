import requests

_cache: dict[str, tuple[float, float] | None] = {}

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "PhillyPools/1.0 (josh@josh-rosenberg.com)"


def geocode_zip(zip_code: str) -> tuple[float, float] | None:
    zip_code = zip_code.strip()
    if zip_code in _cache:
        return _cache[zip_code]

    try:
        response = requests.get(
            NOMINATIM_URL,
            params={"q": zip_code, "countrycodes": "us", "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=5,
        )
        response.raise_for_status()
        results = response.json()
        if results:
            result = (float(results[0]["lat"]), float(results[0]["lon"]))
            _cache[zip_code] = result
            return result
    except Exception:
        pass

    _cache[zip_code] = None
    return None
