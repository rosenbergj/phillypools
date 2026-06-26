import logging
from datetime import timedelta

import requests
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

# NWS event names that trigger the heat banner. Edit this set if you learn
# that only some of these products cause pool schedule changes.
HEAT_EVENT_TYPES = {
    "Heat Advisory",
    "Excessive Heat Warning",
    "Excessive Heat Watch",
}

_PHILLY_ZONE = "PAC101"  # Philadelphia County NWS zone
_NWS_HEADERS = {"User-Agent": "phillypools.app (josh@josh-rosenberg.com)"}


def _seconds_until_midnight() -> int:
    now = timezone.localtime()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60, int((midnight - now).total_seconds()))


def _fetch_nws_heat_alert() -> tuple[bool, bool]:
    """
    Returns (heat_active, api_ok). On network/parse error, returns (False, False)
    so the caller knows not to cache a negative result for a full hour.
    """
    try:
        resp = requests.get(
            "https://api.weather.gov/alerts/active",
            params={"zone": _PHILLY_ZONE},
            headers=_NWS_HEADERS,
            timeout=5,
        )
        resp.raise_for_status()
        features = resp.json().get("features", [])
        active = any(
            f.get("properties", {}).get("event") in HEAT_EVENT_TYPES
            for f in features
            if f.get("properties", {}).get("status") == "Actual"
        )
        return active, True
    except Exception:
        logger.warning("NWS heat alert check failed", exc_info=True)
        return False, False


def is_heat_emergency() -> bool:
    """
    Return True if today is a heat emergency day for Philadelphia.

    Once confirmed True for a calendar day, stays True until midnight ET
    (so the banner persists even if the alert expires mid-afternoon).
    Re-checks NWS at most once per hour when the day hasn't been confirmed yet.
    """
    today = timezone.localdate().isoformat()
    day_key = f"heat_day_{today}"

    if cache.get(day_key):
        return True

    hour = timezone.localtime().hour
    check_key = f"heat_check_{today}_{hour:02d}"
    if cache.get(check_key) is not None:
        return False

    active, api_ok = _fetch_nws_heat_alert()

    if api_ok:
        # Mark that we've done the check for this hour (avoids hammering NWS on
        # every request). Skip caching on API failures so we retry next request.
        cache.set(check_key, True, 3600)

    if active:
        cache.set(day_key, True, _seconds_until_midnight())

    return active
