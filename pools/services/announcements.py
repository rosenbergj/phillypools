from django.utils import timezone

from pools.models import SiteAnnouncement


def get_active_announcement() -> SiteAnnouncement | None:
    """Return the site announcement currently within its start/end window, if any."""
    now = timezone.now()
    return SiteAnnouncement.objects.filter(
        starts_at__lte=now, ends_at__gte=now
    ).order_by("-starts_at").first()
