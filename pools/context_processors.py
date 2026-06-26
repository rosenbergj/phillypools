from pools.services.heat_alert import is_heat_emergency


def heat_emergency_context(request):
    return {"heat_emergency": is_heat_emergency()}
