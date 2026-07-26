import logging

from django.utils import timezone

from pools.services.usage import (
    PAGE_EVENTS,
    classify_device,
    classify_request,
    clean_zip,
    is_probe_path,
    referrer_host,
    ua_family,
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
        if request.method != "GET":
            return
        if response.status_code == 404:
            self._record_probe(request)
            return
        if response.status_code >= 400:
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
            # Only a page navigation carries the headers the forgery check reads;
            # the JSON endpoints below are fetch() calls with a narrower set.
            client_class=classify_request(request, navigation=event in PAGE_EVENTS),
            ua_family=ua_family(user_agent),
            device=classify_device(user_agent),
            referrer_host=referrer_host(request.META.get("HTTP_REFERER", "")),
        )

    def _record_probe(self, request):
        """
        Mark a visitor who asked for something only a vulnerability scanner asks for.

        One row per visitor per day, not one per probe: a scanner works through
        hundreds of paths in a burst, and all the rollup needs from them is the fact
        that this visitor is a scanner — everything else it did that day is discounted
        along with it. The path itself is never stored, only that there was one.
        """
        if not is_probe_path(request.path):
            return

        from pools.models import UsageEvent

        user_agent = request.META.get("HTTP_USER_AGENT", "")
        day = timezone.localdate()
        UsageEvent.objects.get_or_create(
            day=day,
            visitor=visitor_hash(request, day),
            event="probe",
            defaults={
                "client_class": classify_request(request, navigation=True),
                "ua_family": ua_family(user_agent),
                "device": classify_device(user_agent),
            },
        )
