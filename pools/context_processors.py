from pools.services.announcements import COLOR_STYLES, get_active_announcements, render_message
from pools.services.heat_alert import get_current_or_upcoming_emergency


def heat_emergency_context(request):
    emergency = get_current_or_upcoming_emergency()
    press_release = emergency.latest_press_release() if emergency else None
    return {
        "heat_emergency": emergency is not None,
        "heat_emergency_url": press_release.source_url if press_release else None,
    }


def site_announcement_context(request):
    announcements = [
        {
            "html": render_message(a.message),
            "style": COLOR_STYLES[a.color],
            "is_small": a.color == "cyan",
            "show_on_detail_pages": a.show_on_detail_pages,
        }
        for a in get_active_announcements()
    ]
    return {
        "site_announcements": announcements,
        "site_detail_announcements": [a for a in announcements if a["show_on_detail_pages"]],
    }
