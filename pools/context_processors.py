from pools.services.heat_alert import get_current_or_upcoming_emergency


def heat_emergency_context(request):
    emergency = get_current_or_upcoming_emergency()
    press_release = emergency.latest_press_release() if emergency else None
    return {
        "heat_emergency": emergency is not None,
        "heat_emergency_url": press_release.source_url if press_release else None,
    }
