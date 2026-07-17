from django.db.models import Q
from django.utils import timezone

from pools.models import SiteAnnouncement

COLOR_STYLES = {
    "cyan": "background-color: #cff4fc; color: #055160; border-color: #9eeaf9;",
    "yellow": "background-color: #fff3cd; color: #664d03; border-color: #ffe69c;",
    "orange": "background-color: #fd7e14; color: #fff; border-color: #e07214;",
    "red": "background-color: #dc3545; color: #fff; border-color: #bd2130;",
}


def get_active_announcement() -> SiteAnnouncement | None:
    """Return the site announcement currently within its start/end window, if any."""
    now = timezone.now()
    return SiteAnnouncement.objects.filter(
        starts_at__lte=now
    ).filter(
        Q(ends_at__isnull=True) | Q(ends_at__gte=now)
    ).order_by("-starts_at").first()
