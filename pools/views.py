from math import radians, sin, cos, sqrt, atan2

from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone

from pools.models import Pool, Submission
from pools.services.geocoder import geocode_zip, get_zip_polygon
from pools.services.neighborhoods import get_neighborhoods, get_neighborhood_centroid, get_neighborhood_geometry


def _haversine_miles(lat1, lon1, lat2, lon2):
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def _point_in_ring(lon, lat, ring):
    """Ray-casting point-in-polygon. Coordinates are [lon, lat] pairs."""
    inside = False
    j = len(ring) - 1
    for i, (xi, yi) in enumerate(ring):
        xj, yj = ring[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _point_to_segment_miles(lat, lon, alat, alon, blat, blon):
    """Distance from (lat,lon) to the nearest point on segment (a)→(b)."""
    dx, dy = blon - alon, blat - alat
    if dx == 0 and dy == 0:
        return _haversine_miles(lat, lon, alat, alon)
    t = max(0.0, min(1.0, ((lon - alon) * dx + (lat - alat) * dy) / (dx * dx + dy * dy)))
    return _haversine_miles(lat, lon, alat + t * dy, alon + t * dx)


def _distance_to_geometry(lat, lon, geometry) -> float | None:
    """
    Returns 0 if (lat, lon) is inside the geometry, the distance in miles to
    the nearest boundary point if outside, or None if geometry is not a polygon.
    """
    geom_type = geometry.get("type")
    if geom_type == "Polygon":
        polys = [geometry["coordinates"]]
    elif geom_type == "MultiPolygon":
        polys = geometry["coordinates"]
    else:
        return None  # Point, LineString, etc. — caller falls back to centroid

    # Point-in-polygon: check outer ring of each polygon
    for rings in polys:
        if _point_in_ring(lon, lat, rings[0]):
            return 0.0

    # Minimum distance to any edge across all rings
    min_dist = float("inf")
    for rings in polys:
        for ring in rings:
            for i in range(len(ring) - 1):
                alon, alat = ring[i]
                blon, blat = ring[i + 1]
                d = _point_to_segment_miles(lat, lon, alat, alon, blat, blon)
                if d < min_dist:
                    min_dist = d
    return min_dist


def _pool_status_label(pool, today):
    """Return (text, color, bold) for the list label, or (None, None, None)."""
    if not pool.is_active:
        return None, None, None
    if pool.opening_date:
        delta = (pool.opening_date - today).days
        if delta == 0:
            return "Opening today!", "#198754", True
        if delta == 1:
            return "Opening tomorrow!", "#fd7e14", True
        if 2 <= delta <= 5:
            return f"Opening in {delta} days", "#fd7e14", True
        if delta > 5:
            return f"Opening {pool.opening_date.strftime('%-m/%y')}", "#6c757d", False
    if pool.closing_date:
        delta = (pool.closing_date - today).days
        if delta == 0:
            return "Last day \U0001f622", "#dc3545", True
        if delta == 1:
            return "Closing tomorrow", "#fd7e14", True
        if 2 <= delta <= 5:
            return f"Closing in {delta} days", "#fd7e14", True
    return None, None, None


def _pool_map_status(pool, today):
    if not pool.is_active:
        return "inactive"
    if pool.opening_date and pool.opening_date <= today:
        if not pool.closing_date or pool.closing_date >= today:
            return "open"
    if pool.opening_date and pool.opening_date > today:
        return "opening_soon"
    return "no_date"


def index(request):
    pools = list(Pool.objects.all())

    zip_query = request.GET.get("zip", "").strip()
    status_filter = request.GET.get("status", "")
    neighborhood_filter = request.GET.get("neighborhood", "")

    zip_center = None
    zip_error = None
    center_label = None
    boundary_geometry = None  # GeoJSON geometry to outline on the map

    # Determine sort center: zip takes priority, then neighborhood
    if zip_query:
        coords = geocode_zip(zip_query)
        if coords:
            zip_center = coords
            center_label = zip_query
            boundary_geometry = get_zip_polygon(zip_query)
        else:
            zip_error = f'Could not find zip code "{zip_query}".'
    elif neighborhood_filter:
        coords = get_neighborhood_centroid(neighborhood_filter)
        if coords:
            zip_center = coords
            center_label = neighborhood_filter
            boundary_geometry = get_neighborhood_geometry(neighborhood_filter)

    if zip_center:
        for pool in pools:
            if pool.latitude and pool.longitude:
                if boundary_geometry:
                    d = _distance_to_geometry(pool.latitude, pool.longitude, boundary_geometry)
                    pool.distance = d if d is not None else _haversine_miles(
                        zip_center[0], zip_center[1], pool.latitude, pool.longitude
                    )
                else:
                    pool.distance = _haversine_miles(
                        zip_center[0], zip_center[1], pool.latitude, pool.longitude
                    )
            else:
                pool.distance = float("inf")
        pools.sort(key=lambda p: p.distance)
    else:
        pools.sort(key=lambda p: p.name)

    today = timezone.localdate()
    if status_filter == "open":
        pools = [p for p in pools if _pool_map_status(p, today) == "open"]
    elif status_filter == "closed":
        pools = [p for p in pools if _pool_map_status(p, today) not in ("open",)]
    elif status_filter == "active":
        pools = [p for p in pools if p.is_active]
    elif status_filter == "opening_soon":
        pools = [p for p in pools if _pool_map_status(p, today) in ("open", "opening_soon")]

    for pool in pools:
        pool.label_text, pool.label_color, pool.label_bold = _pool_status_label(pool, today)

    neighborhoods = get_neighborhoods()

    pools_geojson = [
        {
            "id": p.id,
            "name": p.name,
            "lat": p.latitude,
            "lng": p.longitude,
            "status": _pool_map_status(p, today),
            "address": p.address,
        }
        for p in pools
        if p.latitude and p.longitude
    ]

    return render(request, "pools/index.html", {
        "pools": pools,
        "pools_geojson": pools_geojson,
        "zip_query": zip_query,
        "zip_center_json": list(zip_center) if zip_center else None,
        "boundary_geometry": boundary_geometry,
        "zip_error": zip_error,
        "status_filter": status_filter,
        "neighborhood_filter": neighborhood_filter,
        "neighborhoods": neighborhoods,
        "show_distance": bool(zip_center),
        "center_label": center_label,
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
        uploaded_image = request.FILES.get("image")

        # Verify Turnstile token if configured
        from django.conf import settings as django_settings
        turnstile_secret = django_settings.CLOUDFLARE_TURNSTILE_SECRET_KEY
        if turnstile_secret:
            token = request.POST.get("cf-turnstile-response", "")
            import requests as http_requests
            resp = http_requests.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": turnstile_secret, "response": token},
                timeout=5,
            )
            if not resp.json().get("success"):
                return render(request, "pools/submit.html", {
                    "pools": pools,
                    "error": "Human verification failed. Please try again.",
                    "form_url": url,
                    "submitter_note": submitter_note,
                    "preselected_pool_id": pool_id,
                    "turnstile_site_key": django_settings.CLOUDFLARE_TURNSTILE_SITE_KEY,
                })

        # Require either a URL or an image
        url_valid = url and (url.startswith("http://") or url.startswith("https://"))
        if not url_valid and not uploaded_image:
            return render(request, "pools/submit.html", {
                "pools": pools,
                "error": "Please provide a link or upload a screenshot.",
                "form_url": url,
                "submitter_note": submitter_note,
                "preselected_pool_id": pool_id,
                "turnstile_site_key": django_settings.CLOUDFLARE_TURNSTILE_SITE_KEY,
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
        pool_list = list(Pool.objects.filter(is_active=True).values("id", "name"))

        # Build a Submission instance so we can save the image via Django's storage
        submission = Submission(
            url=url if url_valid else "",
            submitter_note=submitter_note,
            parsed_pool=parsed_pool,
        )
        if uploaded_image:
            submission.uploaded_image = uploaded_image

        submission.save()

        if url_valid:
            try:
                from pools.services.url_fetcher import fetch_url
                raw_content = fetch_url(url)
            except Exception:
                pass

            try:
                from pools.services.llm_parser import parse_submission
                parsed_fields = parse_submission(raw_content, pool_list)
                llm_response = parsed_fields.pop("_raw", None)
            except Exception:
                pass

        elif uploaded_image and submission.uploaded_image:
            try:
                from pools.services.llm_parser import parse_image_submission
                image_bytes = submission.uploaded_image.read()
                image_name = submission.uploaded_image.name
                parsed_fields = parse_image_submission(image_bytes, image_name, pool_list)
                llm_response = parsed_fields.pop("_raw", None)
            except Exception:
                pass

        if not parsed_pool and parsed_fields.get("pool_id"):
            try:
                parsed_pool = Pool.objects.get(pk=parsed_fields["pool_id"])
            except Pool.DoesNotExist:
                pass

        submission.raw_fetched_content = raw_content
        submission.llm_response = llm_response
        submission.parsed_pool = parsed_pool
        submission.parsed_opening_date = parsed_fields.get("opening_date")
        submission.parsed_closing_date = parsed_fields.get("closing_date")
        submission.parsed_hours = parsed_fields.get("hours") or ""
        submission.parsed_weekday_schedule = parsed_fields.get("weekday_schedule") or ""
        submission.parsed_weekend_schedule = parsed_fields.get("weekend_schedule") or ""
        submission.parsed_notes = parsed_fields.get("notes") or ""
        submission.llm_confidence = parsed_fields.get("confidence", "")
        submission.save()

        return redirect("submit_thanks")

    from django.conf import settings as django_settings
    return render(request, "pools/submit.html", {
        "pools": pools,
        "preselected_pool_id": preselected_pool_id,
        "turnstile_site_key": django_settings.CLOUDFLARE_TURNSTILE_SITE_KEY,
    })


def submit_thanks(request):
    return render(request, "pools/submit_thanks.html")
