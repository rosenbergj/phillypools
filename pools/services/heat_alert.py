from django.db.models import Q
from django.utils import timezone

from pools.models import HeatHealthEmergency


def get_current_or_upcoming_emergency() -> HeatHealthEmergency | None:
    """Return the Heat Health Emergency currently in effect, or one starting later today, if any."""
    now = timezone.now()
    today = timezone.localtime(now).date()
    return HeatHealthEmergency.objects.filter(
        Q(starts_at__lte=now) | Q(starts_at__date=today)
    ).filter(
        Q(ends_at__isnull=True) | Q(ends_at__gte=now)
    ).order_by("starts_at").first()


def is_heat_emergency() -> bool:
    """Return True if Philadelphia currently has a Heat Health Emergency in effect, or one starts later today."""
    return get_current_or_upcoming_emergency() is not None
