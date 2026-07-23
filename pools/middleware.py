import logging

from django.utils import timezone

from pools.services.usage import (
    classify_client,
    classify_device,
    clean_zip,
    referrer_host,
    visitor_hash,
)

logger = logging.getLogger(__name__)

# Which named URL each recorded event corresponds to. Anything not listed here
# (robots.txt, the sitemap, the like endpoint whose data is already in PoolLike)
# is not recorded at all.
_EVENT_BY_URL_NAME = {
    "index": "index",
    "pool_detail": "pool_view",
    "pools_json": "filter",
    "neighborhood_at": "map_pick",
    "submit": "submit_view",
    "submit_thanks": "submit_done",
}


class UsageMiddleware:
    """
    Records the requests the site already makes, so they can be counted later.

    Must sit below WhiteNoiseMiddleware in the stack: WhiteNoise answers static
    file requests without calling further down, which is exactly the filtering we
    want — CSS and favicon hits never reach this and never bloat the table.

    Recording is strictly best-effort. A measurement failure must never turn into
    a failed page load, so everything here is wrapped and swallowed.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            self._record(request, response)
        except Exception:
            logger.exception("usage recording failed")
        return response

    def _record(self, request, response):
        if request.method != "GET" or response.status_code >= 400:
            return
        match = getattr(request, "resolver_match", None)
        if match is None:
            return
        event = _EVENT_BY_URL_NAME.get(match.url_name)
        if event is None:
            return

        from pools.models import UsageEvent

        user_agent = request.META.get("HTTP_USER_AGENT", "")
        day = timezone.localdate()
        UsageEvent.objects.create(
            day=day,
            event=event,
            key=match.kwargs.get("slug", "")[:100],
            status_filter=request.GET.get("status", "")[:20],
            neighborhood=request.GET.get("neighborhood", "")[:100],
            zip_searched=clean_zip(request.GET.get("zip", "")),
            visitor=visitor_hash(request, day),
            client_class=classify_client(user_agent),
            device=classify_device(user_agent),
            referrer_host=referrer_host(request.META.get("HTTP_REFERER", "")),
        )
