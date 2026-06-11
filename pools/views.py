import json
from math import radians, sin, cos, sqrt, atan2

from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone

from pools.models import Pool, Submission
from pools.services.geocoder import geocode_zip


def _haversine_miles(lat1, lon1, lat2, lon2):
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def index(request):
    pools = list(Pool.objects.filter(is_active=True))

    zip_query = request.GET.get("zip", "").strip()
    status_filter = request.GET.get("status", "")
    neighborhood_filter = request.GET.get("neighborhood", "")

    zip_center = None
    zip_error = None

    if zip_query:
        coords = geocode_zip(zip_query)
        if coords:
            zip_center = coords
            for pool in pools:
                if pool.latitude and pool.longitude:
                    pool.distance = _haversine_miles(
                        coords[0], coords[1], pool.latitude, pool.longitude
                    )
                else:
                    pool.distance = float("inf")
            pools.sort(key=lambda p: p.distance)
        else:
            zip_error = f'Could not find zip code "{zip_query}".'
            pools.sort(key=lambda p: p.name)
    else:
        pools.sort(key=lambda p: p.name)

    today = timezone.localdate()
    if status_filter == "open":
        pools = [p for p in pools if p.opening_date and p.closing_date and p.opening_date <= today <= p.closing_date]
    elif status_filter == "closed":
        pools = [p for p in pools if not (p.opening_date and p.closing_date and p.opening_date <= today <= p.closing_date)]

    if neighborhood_filter:
        pools = [p for p in pools if p.neighborhood == neighborhood_filter]

    neighborhoods = (
        Pool.objects.filter(is_active=True)
        .exclude(neighborhood="")
        .values_list("neighborhood", flat=True)
        .distinct()
        .order_by("neighborhood")
    )

    pools_geojson = json.dumps([
        {
            "id": p.id,
            "name": p.name,
            "lat": p.latitude,
            "lng": p.longitude,
            "is_open": p.is_open,
            "address": p.address,
        }
        for p in pools
        if p.latitude and p.longitude
    ])

    return render(request, "pools/index.html", {
        "pools": pools,
        "pools_geojson": pools_geojson,
        "zip_query": zip_query,
        "zip_center_json": json.dumps(list(zip_center) if zip_center else None),
        "zip_error": zip_error,
        "status_filter": status_filter,
        "neighborhood_filter": neighborhood_filter,
        "neighborhoods": neighborhoods,
        "show_distance": bool(zip_center),
    })


def pool_detail(request, pk):
    pool = get_object_or_404(Pool, pk=pk, is_active=True)
    today = timezone.localdate()
    schedule_changes = pool.schedule_changes.filter(date_to__gte=today).order_by("date_from")
    return render(request, "pools/detail.html", {
        "pool": pool,
        "schedule_changes": schedule_changes,
    })


def submit(request):
    pools = Pool.objects.filter(is_active=True).order_by("name")
    preselected_pool_id = request.GET.get("pool", "")

    if request.method == "POST":
        url = request.POST.get("url", "").strip()
        submitter_note = request.POST.get("submitter_note", "").strip()
        pool_id = request.POST.get("pool_id", "").strip()

        if not url or not (url.startswith("http://") or url.startswith("https://")):
            return render(request, "pools/submit.html", {
                "pools": pools,
                "error": "Please enter a valid URL starting with http:// or https://",
                "form_url": url,
                "submitter_note": submitter_note,
                "preselected_pool_id": pool_id,
            })

        parsed_pool = None
        if pool_id:
            try:
                parsed_pool = Pool.objects.get(pk=pool_id)
            except Pool.DoesNotExist:
                pass

        raw_content = ""
        llm_response = None
        parsed_fields = {}

        try:
            from pools.services.url_fetcher import fetch_url
            raw_content = fetch_url(url)
        except Exception:
            pass

        try:
            from pools.services.llm_parser import parse_submission
            pool_list = list(Pool.objects.filter(is_active=True).values("id", "name"))
            parsed_fields = parse_submission(raw_content, pool_list)
            llm_response = parsed_fields.pop("_raw", None)
            if not parsed_pool and parsed_fields.get("pool_id"):
                try:
                    parsed_pool = Pool.objects.get(pk=parsed_fields["pool_id"])
                except Pool.DoesNotExist:
                    pass
        except Exception:
            pass

        Submission.objects.create(
            url=url,
            submitter_note=submitter_note,
            raw_fetched_content=raw_content,
            llm_response=llm_response,
            parsed_pool=parsed_pool,
            parsed_opening_date=parsed_fields.get("opening_date"),
            parsed_closing_date=parsed_fields.get("closing_date"),
            parsed_hours=parsed_fields.get("hours", ""),
            parsed_weekday_schedule=parsed_fields.get("weekday_schedule", ""),
            parsed_weekend_schedule=parsed_fields.get("weekend_schedule", ""),
            parsed_notes=parsed_fields.get("notes", ""),
            llm_confidence=parsed_fields.get("confidence", ""),
        )

        return redirect("submit_thanks")

    return render(request, "pools/submit.html", {
        "pools": pools,
        "preselected_pool_id": preselected_pool_id,
    })


def submit_thanks(request):
    return render(request, "pools/submit_thanks.html")
