from django.db.models import Q
from django.utils import timezone

from pools.models import HeatHealthEmergency


def is_heat_emergency() -> bool:
    """Return True if Philadelphia currently has a Heat Health Emergency in effect."""
    now = timezone.now()
    return HeatHealthEmergency.objects.filter(starts_at__lte=now).filter(
        Q(ends_at__isnull=True) | Q(ends_at__gte=now)
    ).exists()
