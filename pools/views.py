import json
import logging
import secrets
import threading
from datetime import timedelta
from math import radians, sin, cos, sqrt, atan2
from pathlib import Path

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from pools.models import Pool, PoolLike, PoolSeasonHistory, ScheduleChange, Submission, UsageDaily, UsageEvent
from pools.services.geocoder import geocode_zip, get_zip_polygon
from pools.services.neighborhoods import get_neighborhoods, get_neighborhood_centroid, get_neighborhood_geometry
from pools.services.usage import (
    USAGE_RAW_RETENTION_DAYS,
    classify_client,
    classify_device,
    get_client_ip as _get_client_ip,
    referrer_host,
    visitor_hash,
)

logger = logging.getLogger(__name__)

LIKE_COOKIE_NAME = "voter_id"
LIKE_COOKIE_MAX_AGE = 60 * 60 * 24 * 365 * 5  # 5 years
LIKE_RATE_LIMIT_WINDOW = timedelta(hours=1)
LIKE_RATE_LIMIT_MAX = 30  # new likes per IP per window, across all pools

# Leading bytes that identify common image formats.
_IMAGE_MAGIC = [
    b"\xff\xd8\xff",            # JPEG
    b"\x89PNG\r\n\x1a\n",       # PNG
    b"GIF87a",                  # GIF
    b"GIF89a",                  # GIF
    b"BM",                      # BMP
    b"II\x2a\x00",              # TIFF (little-endian)
    b"MM\x00\x2a",              # TIFF (big-endian)
]
_WEBP_RIFF = b"RIFF"
_WEBP_MARKER = b"WEBP"
# AVIF/HEIF/HEIC use ISOBMFF: bytes 4-7 are "ftyp", bytes 8-11 are the brand.
# Wix CDN serves enc_avif content with a .jpg filename, so brand-check is needed.
_FTYP_MARKER = b"ftyp"
_AVIF_BRANDS = {b"avif", b"avis", b"mif1", b"miaf", b"heic", b"heix", b"hevc", b"hevx"}


def _is_image_bytes(data: bytes) -> bool:
    for magic in _IMAGE_MAGIC:
        if data.startswith(magic):
            return True
    # WebP: RIFF????WEBP
    if data[:4] == _WEBP_RIFF and data[8:12] == _WEBP_MARKER:
        return True
    # AVIF/HEIF/HEIC: ????ftyp<brand>
    if len(data) >= 12 and data[4:8] == _FTYP_MARKER and data[8:12] in _AVIF_BRANDS:
        return True
    return False


_PHILLY_BOUNDARY = json.loads(
    (Path(__file__).parent.parent / "static" / "philly_boundary.json").read_text()
)


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
            return f"Opening {pool.opening_date.strftime('%a %-m/%-d')}", "#6c757d", False
    if pool.closing_date:
        delta = (pool.closing_date - today).days
        if delta == 0:
            return "Last day \U0001f622", "#dc3545", True
        if delta == 1:
            return "Closing tomorrow", "#fd7e14", True
        if 2 <= delta <= 5:
            return f"Closing in {delta} days", "#fd7e14", True
        if delta > 5:
            return f"Closing {pool.closing_date.strftime('%a %-m/%-d')}", "#6c757d", False
        if delta < 0:
            return f"Closed for the season on {pool.closing_date.strftime('%-m/%-d')}", "#6c757d", False
    return None, None, None


def _season_duration(pool, today):
    """Return e.g. '7 weeks, 3 days' if both dates exist and are from the current year."""
    if not (pool.opening_date and pool.closing_date
            and pool.opening_date.year == today.year
            and pool.closing_date.year == today.year):
        return None
    total = (pool.closing_date - pool.opening_date).days + 1
    if total <= 0:
        return None
    weeks, days = divmod(total, 7)
    parts = []
    if weeks:
        parts.append(f"{weeks} week{'s' if weeks != 1 else ''}")
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    return ", ".join(parts)


def _last_season_duration(pool, last_year):
    """Return e.g. '7 weeks, 3 days' for last_year's recorded season, or '0 days' if
    the pool has no complete opening/closing record for that year (i.e. never opened)."""
    try:
        h = pool.season_history.get(year=last_year)
    except PoolSeasonHistory.DoesNotExist:
        return "0 days"
    if not h.opening_date or not h.closing_date:
        return "0 days"
    total = (h.closing_date - h.opening_date).days + 1
    if total <= 0:
        return "0 days"
    weeks, days = divmod(total, 7)
    parts = []
    if weeks:
        parts.append(f"{weeks} week{'s' if weeks != 1 else ''}")
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    return ", ".join(parts)


def _pool_map_status(pool, today):
    if not pool.is_active:
        return "inactive"
    if pool.opening_date:
        if pool.opening_date.year < today.year:
            return "no_date"  # prior-season data — treat as TBD
        if pool.opening_date <= today:
            if not pool.closing_date or pool.closing_date >= today:
                # An active same-day/short-term schedule change (e.g. emergency
                # closure) isn't "closed" in the season sense, but "open" would be
                # misleading too — show it as opening_soon instead.
                if getattr(pool, "active_schedule_change", None):
                    return "opening_soon"
                return "open"
            return "closed"  # closed this season
        return "opening_soon"
    return "no_date"


def _annotate_active_schedule_changes(pools, today):
    from django.db.models import Q

    changes_by_pool = {}
    changes = ScheduleChange.objects.filter(
        Q(date_to__gte=today) | Q(date_to__isnull=True),
        pool_id__in=[p.id for p in pools], date_from__lte=today,
    ).order_by("date_from")
    for change in changes:
        changes_by_pool.setdefault(change.pool_id, change)
    for pool in pools:
        pool.active_schedule_change = changes_by_pool.get(pool.id)


def _annotate_likes(pools, voter_id: str, year: int):
    from django.db.models import Count

    like_counts = dict(
        PoolLike.objects.filter(year=year).values("pool_id").annotate(c=Count("id")).values_list("pool_id", "c")
    )
    liked_pool_ids = set()
    if voter_id:
        liked_pool_ids = set(
            PoolLike.objects.filter(year=year, voter_id=voter_id).values_list("pool_id", flat=True)
        )
    for pool in pools:
        pool.like_count = like_counts.get(pool.id, 0)
        pool.user_liked = pool.id in liked_pool_ids


def _assemble_pool_data(zip_query: str, neighborhood_filter: str, status_filter: str, voter_id: str = "") -> dict:
    """Filter, sort, and annotate pools. Shared by index and pools_json."""
    pools = list(Pool.objects.all())
    zip_center = None
    zip_error = None
    center_label = None
    boundary_geometry = None

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
    _annotate_active_schedule_changes(pools, today)
    if status_filter == "open":
        pools = [p for p in pools if _pool_map_status(p, today) == "open"]
    elif status_filter == "closed":
        pools = [p for p in pools if _pool_map_status(p, today) not in ("open",)]
    elif status_filter == "active":
        pools = [p for p in pools if p.is_active]
    elif status_filter == "opening_soon":
        pools = [p for p in pools if _pool_map_status(p, today) in ("open", "opening_soon")]
    elif status_filter == "zumba":
        pools = [
            p for p in pools
            if "zumba" in p.weekday_schedule.lower() or "zumba" in p.weekend_schedule.lower()
        ]

    for pool in pools:
        pool.map_status = _pool_map_status(pool, today)
        pool.label_text, pool.label_color, pool.label_bold = _pool_status_label(pool, today)

    _annotate_likes(pools, voter_id, today.year)

    return {
        "pools": pools,
        "zip_center": zip_center,
        "zip_error": zip_error,
        "center_label": center_label,
        "boundary_geometry": boundary_geometry,
        "show_distance": bool(zip_center),
    }


def _thanks_url(pool_id: str) -> str:
    url = reverse("submit_thanks")
    if pool_id and pool_id.isdigit():
        url += f"?pool={pool_id}"
    return url


def index(request):
    zip_query = request.GET.get("zip", "").strip()
    status_filter = request.GET.get("status", "")
    neighborhood_filter = request.GET.get("neighborhood", "")

    voter_id = request.COOKIES.get(LIKE_COOKIE_NAME, "")
    ctx = _assemble_pool_data(zip_query, neighborhood_filter, status_filter, voter_id)
    pools = ctx["pools"]
    zip_center = ctx["zip_center"]

    pools_geojson = [
        {
            "id": p.id,
            "slug": p.slug,
            "name": p.name,
            "lat": p.latitude,
            "lng": p.longitude,
            "status": p.map_status,
            "address": p.address,
            "opening_date": p.opening_date.isoformat() if p.opening_date else None,
            "closing_date": p.closing_date.isoformat() if p.closing_date else None,
            "weekday_schedule": p.weekday_schedule or None,
            "weekend_schedule": p.weekend_schedule or None,
            "active_schedule_change": p.active_schedule_change.description if p.active_schedule_change else None,
            "social_media_url": p.social_media_url or None,
            "phillypublicpools_url": p.phillypublicpools_url or None,
            "like_count": p.like_count,
            "user_liked": p.user_liked,
        }
        for p in pools
        if p.latitude and p.longitude
    ]

    return render(request, "pools/index.html", {
        "pools": pools,
        "pools_geojson": pools_geojson,
        "zip_query": zip_query,
        "zip_center_json": list(zip_center) if zip_center else None,
        "boundary_geometry": ctx["boundary_geometry"],
        "philly_boundary": _PHILLY_BOUNDARY,
        "zip_error": ctx["zip_error"],
        "status_filter": status_filter,
        "neighborhood_filter": neighborhood_filter,
        "neighborhoods": get_neighborhoods(),
        "show_distance": ctx["show_distance"],
        "center_label": ctx["center_label"],
    })


def pools_json(request):
    zip_query = request.GET.get("zip", "").strip()
    status_filter = request.GET.get("status", "")
    neighborhood_filter = request.GET.get("neighborhood", "")

    voter_id = request.COOKIES.get(LIKE_COOKIE_NAME, "")
    ctx = _assemble_pool_data(zip_query, neighborhood_filter, status_filter, voter_id)
    pools = ctx["pools"]

    pools_list = []
    for p in pools:
        dist = getattr(p, "distance", None)
        pools_list.append({
            "id": p.id,
            "slug": p.slug,
            "name": p.name,
            "address": p.address,
            "lat": p.latitude,
            "lng": p.longitude,
            "status": p.map_status,
            "is_active": p.is_active,
            "label_text": p.label_text,
            "label_color": p.label_color,
            "label_bold": p.label_bold,
            "distance": None if (dist is None or dist == float("inf")) else dist,
            "opening_date": p.opening_date.isoformat() if p.opening_date else None,
            "closing_date": p.closing_date.isoformat() if p.closing_date else None,
            "weekday_schedule": p.weekday_schedule or None,
            "weekend_schedule": p.weekend_schedule or None,
            "active_schedule_change": p.active_schedule_change.description if p.active_schedule_change else None,
            "social_media_url": p.social_media_url or None,
            "phillypublicpools_url": p.phillypublicpools_url or None,
            "like_count": p.like_count,
            "user_liked": p.user_liked,
            "pool_type": p.pool_type,
        })

    return JsonResponse({
        "pools": pools_list,
        "show_distance": ctx["show_distance"],
        "center_label": ctx["center_label"],
        "zip_error": ctx["zip_error"],
        "zip_center": list(ctx["zip_center"]) if ctx["zip_center"] else None,
        "boundary_geometry": ctx["boundary_geometry"],
        "neighborhood_filter": neighborhood_filter,
        "zip_query": zip_query,
        "status_filter": status_filter,
    })


def neighborhood_at(request):
    try:
        lat = float(request.GET["lat"])
        lng = float(request.GET["lng"])
    except (KeyError, ValueError):
        return JsonResponse({"neighborhood": None})
    for n in get_neighborhoods():
        geometry = n.get("geometry")
        if not geometry:
            continue
        geom_type = geometry["type"]
        coords = geometry["coordinates"]
        if geom_type == "Polygon":
            if _point_in_ring(lng, lat, coords[0]):
                return JsonResponse({"neighborhood": n["name"]})
        elif geom_type == "MultiPolygon":
            for polygon in coords:
                if _point_in_ring(lng, lat, polygon[0]):
                    return JsonResponse({"neighborhood": n["name"]})
    return JsonResponse({"neighborhood": None})


def pool_detail_pk_redirect(request, pk):
    pool = get_object_or_404(Pool, pk=pk)
    return redirect(pool.get_absolute_url(), permanent=True)


def pool_detail(request, slug):
    from django.db.models import Q

    pool = get_object_or_404(Pool, slug=slug)
    today = timezone.localdate()
    schedule_changes = pool.schedule_changes.filter(
        Q(date_to__gte=today) | Q(date_to__isnull=True)
    ).order_by("date_from")
    pool.active_schedule_change = next((c for c in schedule_changes if c.date_from <= today), None)
    pool.map_status = _pool_map_status(pool, today)
    voter_id = request.COOKIES.get(LIKE_COOKIE_NAME, "")
    year = today.year
    last_year = year - 1

    # The app launched in 2026, so there's no history from before that. Don't show a
    # "last season" stat until 2026 actually is the previous year (i.e. from 2027 on).
    last_season_duration = None
    if last_year >= 2026:
        last_season_duration = _last_season_duration(pool, last_year)

    prior_schedule = None
    if not pool.weekday_schedule and not pool.weekend_schedule:
        try:
            h = pool.season_history.get(year=year - 1)
            if h.weekday_schedule or h.weekend_schedule:
                prior_schedule = h
        except PoolSeasonHistory.DoesNotExist:
            pass

    return render(request, "pools/detail.html", {
        "pool": pool,
        "schedule_changes": schedule_changes,
        "season_duration": _season_duration(pool, today),
        "last_season_duration": last_season_duration,
        "last_season_year": last_year,
        "like_count": pool.likes.filter(year=year).count(),
        "total_like_count": pool.likes.count(),
        "user_liked": bool(voter_id) and pool.likes.filter(voter_id=voter_id, year=year).exists(),
        "prior_schedule": prior_schedule,
    })


def toggle_like(request, pk):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    pool = get_object_or_404(Pool, pk=pk)
    voter_id = request.COOKIES.get(LIKE_COOKIE_NAME, "")
    is_new_voter = not voter_id
    if is_new_voter:
        voter_id = secrets.token_hex(16)
    year = timezone.localdate().year

    existing = PoolLike.objects.filter(pool=pool, voter_id=voter_id, year=year).first()
    if existing:
        existing.delete()
        liked = False
    else:
        ip = _get_client_ip(request)
        recent_count = PoolLike.objects.filter(
            ip_address=ip, created_at__gte=timezone.now() - LIKE_RATE_LIMIT_WINDOW
        ).count()
        if recent_count >= LIKE_RATE_LIMIT_MAX:
            return JsonResponse({"error": "Too many likes, please slow down."}, status=429)
        PoolLike.objects.create(pool=pool, voter_id=voter_id, ip_address=ip, year=year)
        liked = True

    response = JsonResponse({"liked": liked, "like_count": pool.likes.filter(year=year).count()})
    if is_new_voter:
        response.set_cookie(
            LIKE_COOKIE_NAME, voter_id,
            max_age=LIKE_COOKIE_MAX_AGE, samesite="Lax", httponly=True,
        )
    return response


def _process_submission_llm(submission_pk, url, has_image):
    """Run URL fetch and LLM parsing in a background thread after the submission is saved."""
    from django import db
    try:
        from pools.services.llm_parser import build_pool_list
        pool_list = build_pool_list()

        submission = Submission.objects.get(pk=submission_pk)
        raw_content = ""
        llm_response = None
        parsed_fields = {}

        if url:
            try:
                from pools.services.url_fetcher import fetch_url
                raw_content = fetch_url(url)
            except Exception:
                pass
            try:
                from pools.services.llm_parser import parse_submission
                parsed_fields = parse_submission(raw_content, pool_list)
                llm_response = parsed_fields.pop("_raw", None)
            except Exception as e:
                llm_response = {"error": str(e)}

        elif has_image and submission.uploaded_image:
            try:
                from pools.services.llm_parser import parse_image_submission
                image_bytes = submission.uploaded_image.read()
                image_name = submission.uploaded_image.name
                parsed_fields = parse_image_submission(image_bytes, image_name, pool_list)
                llm_response = parsed_fields.pop("_raw", None)
            except Exception as e:
                llm_response = {"error": str(e)}

        parsed_pool = submission.parsed_pool
        if not parsed_pool and parsed_fields.get("pool_id"):
            try:
                parsed_pool = Pool.objects.get(pk=parsed_fields["pool_id"])
            except Pool.DoesNotExist:
                pass

        parsed_notes = parsed_fields.get("notes") or ""
        if parsed_fields.get("stale_year_warning"):
            parsed_notes = "WARNING: Source may be from a prior season — verify dates before applying.\n" + parsed_notes

        submission.raw_fetched_content = raw_content
        submission.llm_response = llm_response
        submission.parsed_pool = parsed_pool
        submission.parsed_opening_date = parsed_fields.get("opening_date")
        submission.parsed_closing_date = parsed_fields.get("closing_date")
        submission.parsed_weekday_schedule = parsed_fields.get("weekday_schedule") or ""
        submission.parsed_weekend_schedule = parsed_fields.get("weekend_schedule") or ""
        submission.parsed_notes = parsed_notes
        submission.llm_confidence = parsed_fields.get("confidence", "")
        submission.save()
    except Exception:
        logger.exception("Background LLM processing failed for submission %s", submission_pk)
    finally:
        db.connection.close()


def submit(request):
    pools = Pool.objects.all().order_by("name")
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

        # Require either a valid URL or an image
        url_valid = False
        if url:
            from django.core.validators import URLValidator
            from django.core.exceptions import ValidationError
            try:
                URLValidator(schemes=["http", "https"])(url)
                url_valid = True
            except ValidationError:
                pass
        if not url_valid and not uploaded_image:
            return render(request, "pools/submit.html", {
                "pools": pools,
                "error": "Please provide a valid link (starting with http:// or https://) or upload a screenshot.",
                "form_url": url,
                "submitter_note": submitter_note,
                "preselected_pool_id": pool_id,
                "turnstile_site_key": django_settings.CLOUDFLARE_TURNSTILE_SITE_KEY,
            })

        # Content moderation: check uploaded images before saving anything to storage.
        # Silently drop flagged submissions — don't reveal to the submitter that they were caught.
        if uploaded_image:
            from pools.services.llm_parser import moderate_image
            image_bytes_for_check = uploaded_image.read()
            uploaded_image.seek(0)
            ip = _get_client_ip(request)
            if not _is_image_bytes(image_bytes_for_check):
                logger.warning(
                    "Rejected non-image upload: filename=%r ip=%s",
                    uploaded_image.name, ip,
                )
                return redirect(_thanks_url(pool_id))
            if moderate_image(image_bytes_for_check, uploaded_image.name):
                logger.warning(
                    "Rejected image flagged by moderation: filename=%r ip=%s",
                    uploaded_image.name, ip,
                )
                return redirect(_thanks_url(pool_id))

        parsed_pool = None
        if pool_id:
            try:
                parsed_pool = Pool.objects.get(pk=pool_id)
            except Pool.DoesNotExist:
                pass

        # Save submission immediately so the user can be redirected to the thanks page.
        # URL fetch and LLM parsing happen in a background thread.
        submission = Submission(
            url=url if url_valid else "",
            submitter_note=submitter_note,
            parsed_pool=parsed_pool,
        )
        if uploaded_image:
            submission.uploaded_image = uploaded_image
        submission.save()

        threading.Thread(
            target=_process_submission_llm,
            args=(submission.pk, url if url_valid else "", bool(uploaded_image)),
            daemon=True,
        ).start()

        return redirect(_thanks_url(pool_id))

    from django.conf import settings as django_settings
    return render(request, "pools/submit.html", {
        "pools": pools,
        "preselected_pool_id": preselected_pool_id,
        "turnstile_site_key": django_settings.CLOUDFLARE_TURNSTILE_SITE_KEY,
    })


def submit_thanks(request):
    pool_id = request.GET.get("pool", "")
    return render(request, "pools/submit_thanks.html", {"pool_id": pool_id if pool_id.isdigit() else ""})


# Map pin clicks never reach the server on their own, so this is the one place we
# ask the browser to tell us something. Cap what a single visitor can report in a
# day: the endpoint is unauthenticated and writable by anyone, so without a limit
# it is trivial to inflate a pool's numbers or pad the table.
PIN_CLICK_DAILY_MAX = 400


@require_POST
def record_pin_click(request):
    """
    Record that a map pin was clicked. CSRF-protected like the like button, and
    silently ignores anything it can't make sense of — a measurement endpoint
    should never give a caller a reason to retry or a signal to probe with.
    """
    slug = request.POST.get("slug", "")[:100]
    if not slug or not Pool.objects.filter(slug=slug).exists():
        return HttpResponse(status=204)

    day = timezone.localdate()
    visitor = visitor_hash(request, day)
    already = UsageEvent.objects.filter(day=day, visitor=visitor, event="pin_click").count()
    if already >= PIN_CLICK_DAILY_MAX:
        return HttpResponse(status=429)

    user_agent = request.META.get("HTTP_USER_AGENT", "")
    UsageEvent.objects.create(
        day=day,
        event="pin_click",
        key=slug,
        visitor=visitor,
        client_class=classify_client(user_agent),
        device=classify_device(user_agent),
        referrer_host=referrer_host(request.META.get("HTTP_REFERER", "")),
    )
    return HttpResponse(status=204)


def _daily_series(rows, days):
    """Turn (day -> count) rows into a dense list over `days`, so gaps read as zeroes."""
    by_day = {r["day"]: r for r in rows}
    today = timezone.localdate()
    out = []
    for offset in range(days - 1, -1, -1):
        d = today - timedelta(days=offset)
        row = by_day.get(d)
        out.append({
            "day": d,
            "visitors": row["visitors"] if row else 0,
            "events": row["events"] if row else 0,
        })
    return out


def _top(day_from, metric, limit=15):
    """Ranked breakdown for one metric, with each row's bar width relative to the leader."""
    rows = list(
        UsageDaily.objects.filter(day__gte=day_from, metric=metric)
        .values("key")
        .annotate(visitors=Sum("visitors"), events=Sum("events"))
        .order_by("-visitors")[:limit]
    )
    top = max([r["visitors"] for r in rows] + [1])
    for row in rows:
        row["pct"] = round(100 * row["visitors"] / top, 1)
        row["label"] = row["key"]
    return rows


@staff_member_required
def stats(request):
    try:
        days = max(1, min(365, int(request.GET.get("days", 30))))
    except ValueError:
        days = 30
    day_from = timezone.localdate() - timedelta(days=days - 1)

    visitors = list(
        UsageDaily.objects.filter(day__gte=day_from, metric="visitors", key="")
        .values("day", "visitors", "events")
    )
    confirmed = list(
        UsageDaily.objects.filter(day__gte=day_from, metric="visitors", key="js_confirmed")
        .values("day", "visitors", "events")
    )
    bots = list(
        UsageDaily.objects.filter(day__gte=day_from, metric="visitors", key="bot")
        .values("day", "visitors", "events")
    )

    series = _daily_series(visitors, days)
    confirmed_by_day = {r["day"]: r["visitors"] for r in confirmed}
    bots_by_day = {r["day"]: r["visitors"] for r in bots}
    peak = max([r["visitors"] for r in series] + [1])
    for row in series:
        row["confirmed"] = confirmed_by_day.get(row["day"], 0)
        row["bots"] = bots_by_day.get(row["day"], 0)
        # Bar heights as a share of the busiest day. `confirmed` is a subset of
        # `visitors`, so it's drawn nested inside the same bar, never stacked on
        # top of it — stacking would imply a total that double-counts people.
        row["pct"] = round(100 * row["visitors"] / peak, 1)
        row["confirmed_pct"] = round(100 * row["confirmed"] / peak, 1)

    # Pool rows are keyed by slug so the rollup needs no join. Resolve to names here,
    # falling back to the raw slug for a pool that has since been renamed or removed —
    # last season's history keeps whatever slug was current when it was recorded.
    pools_by_slug = {p.slug: p.name for p in Pool.objects.all()}
    pool_views = _top(day_from, "pool_view")
    pin_clicks = _top(day_from, "pin_click")
    for row in pool_views + pin_clicks:
        row["label"] = pools_by_slug.get(row["key"], row["key"])

    event_names = dict(UsageEvent.EVENT_CHOICES)
    events_by_type = _top(day_from, "event")
    for row in events_by_type:
        row["label"] = event_names.get(row["key"], row["key"])

    return render(request, "pools/stats.html", {
        "days": days,
        "series": series,
        "totals": {
            "visitors": sum(r["visitors"] for r in series),
            "confirmed": sum(r["confirmed"] for r in series),
            "bots": sum(r["bots"] for r in series),
            "events": sum(r["events"] for r in series),
        },
        "peak_visitors": peak,
        "first_day": day_from,
        "last_day": timezone.localdate(),
        "pool_views": pool_views,
        "pin_clicks": pin_clicks,
        "status_filters": _top(day_from, "status_filter"),
        "neighborhoods": _top(day_from, "neighborhood"),
        "zips": _top(day_from, "zip"),
        "referrers": _top(day_from, "referrer"),
        "devices": _top(day_from, "device"),
        "events_by_type": events_by_type,
        "raw_retention_days": USAGE_RAW_RETENTION_DAYS,
        "raw_rows": UsageEvent.objects.count(),
    })
