"""Reading pool records from the city's ArcGIS feature service.

`scrape_pools` and `rescrape_inactive` read the same dataset through the ArcGIS Hub
GeoJSON *download* endpoint, which serves a periodically-regenerated export. This
module talks to the underlying FeatureServer query API instead, because a change
detector wants the live table rather than a cached export of it.

The GIS layer is the city's own asset system of record, and it keeps being edited
after the seasonal press releases stop changing — Amos Pool's 7/31/2026 reopening
reached GIS but never reached the opening-schedule page, which had frozen on July 1.
That gap is the reason this exists.
"""
import datetime
import json
import time

import requests

from pools.services.user_agents import CRAWLER_HEADERS

# Human-readable dataset page. Applied submissions put this in the pool's
# *_source_url, which renders as a public "[link]" on the pool detail page, so it
# has to be something a visitor can actually read — not a JSON query URL.
DATASET_PAGE_URL = "https://opendataphilly.org/datasets/ppr-swimming-pools/"

FEATURE_SERVER_URL = (
    "https://services.arcgis.com/fLeGjb7u4uXqeF9q/arcgis/rest/services/"
    "PPR_Swimming_Pools/FeatureServer/0"
)

# The FeatureServer intermittently answers a valid query with
# {"code": 400, "message": "Invalid URL"} — its own hiccup, not a bad request from
# us — and a retry seconds later succeeds. Retrying in-process removes most of
# those before anything downstream ever sees a failure.
_FETCH_RETRY_BACKOFF = (2, 5)

# How many check runs in a row must fail before a fetch failure is worth an email.
# The cron runs five times a day, so this is most of a day of a dead source; below
# it the failure is a note, which shows up in the cron log and nowhere else.
FAILURE_ALERT_THRESHOLD = 4

# Pool date field -> the GIS attribute it comes from. The layer carries no closing
# date today; if the city ever adds one, map it here and everything downstream —
# detection, submissions, review — starts handling it with no other change.
# `detect_unmapped_date_fields` watches for that field appearing.
DATE_FIELD_MAP = {
    "opening_date": "pool_open_date",
    # "closing_date": "pool_close_date",
}

# Substrings that suggest a layer field carries a closing/end date we don't map yet.
_CLOSE_FIELD_HINTS = ("close", "closing", "end_date", "enddate", "season_end")

ATTRIBUTES = [
    "ppr_amenity_id",
    "pool_name",
    "pool_status",
    "comments",
    "address_911",
    *DATE_FIELD_MAP.values(),
]


def epoch_ms_to_date(value):
    """ArcGIS serves dates as epoch milliseconds at UTC midnight. Converting in local
    time would shift them a day for anyone west of UTC, so this is explicitly UTC."""
    if value in (None, ""):
        return None
    return datetime.datetime.fromtimestamp(value / 1000, datetime.UTC).date()


def _get_json(url, params, timeout):
    """GET one ArcGIS endpoint, retrying transient failures. Raises the last error."""
    for attempt in range(len(_FETCH_RETRY_BACKOFF) + 1):
        try:
            resp = requests.get(url, params=params, headers=CRAWLER_HEADERS, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()
            if "error" in payload:
                raise RuntimeError(f"ArcGIS error: {payload['error']}")
            return payload
        except Exception:
            if attempt == len(_FETCH_RETRY_BACKOFF):
                raise
            time.sleep(_FETCH_RETRY_BACKOFF[attempt])


def fetch_features(timeout=30):
    """Return the layer's records as a list of attribute dicts. Raises on failure;
    callers turn that into a report error."""
    payload = _get_json(
        f"{FEATURE_SERVER_URL}/query",
        {
            "where": "1=1",
            "outFields": ",".join(ATTRIBUTES),
            "returnGeometry": "false",
            "f": "json",
        },
        timeout,
    )
    return [f.get("attributes", {}) for f in payload.get("features", [])]


def fetch_layer_field_names(timeout=30):
    """Field names defined on the layer, used to notice new date columns."""
    payload = _get_json(FEATURE_SERVER_URL, {"f": "json"}, timeout)
    return [f["name"] for f in payload.get("fields", [])]


def detect_unmapped_date_fields(field_names):
    """Layer fields that look like a closing/end date but aren't mapped yet.

    The city has no closing-date column today, and its absence is the single biggest
    gap in this source. Rather than requiring someone to notice by hand, the check
    reports the day one shows up."""
    mapped = set(DATE_FIELD_MAP.values())
    return [
        name
        for name in field_names
        if name not in mapped
        and any(hint in name.lower() for hint in _CLOSE_FIELD_HINTS)
    ]


def is_inactive(attrs):
    """Match `scrape_pools`' reading of pool_status so the two agree on the word."""
    status = (attrs.get("pool_status") or "").lower()
    return "inactive" in status or "closed" in status


def record_snapshot(attrs):
    """Readable dump of one GIS record, stored on the submission so the reviewer can
    see the whole record — status, comments, address — not just the changed date."""
    readable = dict(attrs)
    for gis_field in DATE_FIELD_MAP.values():
        if gis_field in readable:
            value = epoch_ms_to_date(readable[gis_field])
            readable[gis_field] = value.isoformat() if value else None
    amenity_id = attrs.get("ppr_amenity_id") or ""
    query_url = (
        f"{FEATURE_SERVER_URL}/query?where=ppr_amenity_id%3D%27{amenity_id}%27"
        "&outFields=*&f=json"
    )
    return f"Source record: {query_url}\n\n{json.dumps(readable, indent=2, sort_keys=True)}"


# --- change detection ------------------------------------------------------


def _describe(field, old, new):
    return f"{field.replace('_', ' ')} {old or 'none'} → {new}"


def check_pool_gis(dry_run=False):
    """Compare the GIS layer against every pool and raise submissions for the
    differences. Never raises — problems come back as `errors` on the report."""
    from django.utils import timezone

    from pools.models import GisCheckState, Pool, PoolGisState, Submission
    from pools.services.page_monitor import CheckReport

    report = CheckReport()

    try:
        features = fetch_features()
    except Exception as e:
        _report_fetch_failure(report, e, dry_run, GisCheckState, timezone.now())
        return report

    if not dry_run:
        recovered = GisCheckState.load().record_success()
        if recovered:
            report.notes.append(
                f"GIS fetch recovered after {recovered} failure(s) in a row"
            )

    try:
        unmapped = detect_unmapped_date_fields(fetch_layer_field_names())
        if unmapped:
            report.highlights.append(
                "GIS layer has date field(s) we don't map yet: "
                f"{', '.join(unmapped)} — add to DATE_FIELD_MAP in pools/services/gis.py"
            )
    except Exception as e:
        # Non-fatal: the records themselves already fetched fine.
        report.notes.append(f"Could not read GIS layer fields: {e}")

    by_amenity_id = {
        str(a.get("ppr_amenity_id") or ""): a
        for a in features
        if a.get("ppr_amenity_id")
    }

    now = timezone.now()
    proposed = skipped_no_id = unmatched = 0

    for pool in Pool.objects.exclude(ppr_amenity_id="").order_by("name"):
        attrs = by_amenity_id.get(pool.ppr_amenity_id)
        if attrs is None:
            unmatched += 1
            continue

        if dry_run:
            # An unsaved instance still answers the "have we proposed this?" question
            # without leaving a row behind.
            state = PoolGisState.objects.filter(pool=pool).first() or PoolGisState(pool=pool)
        else:
            state, _ = PoolGisState.objects.get_or_create(pool=pool)
        gis_dates = {
            our_field: epoch_ms_to_date(attrs.get(gis_field))
            for our_field, gis_field in DATE_FIELD_MAP.items()
        }
        status = (attrs.get("pool_status") or "").strip()

        changed_in_gis = any(
            getattr(state, f"gis_{f}") != v for f, v in gis_dates.items()
        ) or state.gis_status != status

        # An inactive record's dates describe a season that isn't happening; record
        # what GIS says but don't propose it as this pool's opening date.
        differences = []
        if not is_inactive(attrs):
            for our_field, gis_value in gis_dates.items():
                if gis_value is None:
                    continue
                if gis_value == getattr(pool, our_field):
                    continue
                if gis_value == getattr(state, f"proposed_{our_field}"):
                    continue  # already put to review; don't nag until GIS moves again
                differences.append((our_field, gis_value))

        if differences and not dry_run:
            _create_gis_submission(pool, attrs, differences, Submission)
        if differences:
            proposed += 1
            summary = ", ".join(
                _describe(f, getattr(pool, f), v) for f, v in differences
            )
            verb = "Would propose" if dry_run else "Proposed"
            report.highlights.append(f"{verb} for {pool.name}: {summary}")

        if not dry_run:
            for our_field, gis_value in gis_dates.items():
                setattr(state, f"gis_{our_field}", gis_value)
            for our_field, gis_value in differences:
                setattr(state, f"proposed_{our_field}", gis_value)
            state.gis_status = status
            state.last_checked = now
            if changed_in_gis:
                state.last_changed = now
            state.save()

    skipped_no_id = Pool.objects.filter(ppr_amenity_id="").count()
    if skipped_no_id:
        report.notes.append(
            f"Skipped {skipped_no_id} pool(s) with no ppr_amenity_id — not matchable in GIS"
        )
    if unmatched:
        report.notes.append(f"{unmatched} pool(s) had an amenity ID absent from GIS")
    if not proposed:
        report.notes.append(f"GIS agrees with our data ({len(by_amenity_id)} records checked)")

    return report


def _report_fetch_failure(report, error, dry_run, GisCheckState, now):
    """Record a failed fetch, and decide whether it's worth waking anyone up.

    A single failure is almost always the FeatureServer's own hiccup and clears by
    the next run, so it lands in `notes`. Only a streak — the source really being
    gone — goes in `errors`, which is what the digest email is built from."""
    if dry_run:
        report.errors.append(f"GIS fetch failed: {error}")
        return

    state = GisCheckState.load()
    state.record_failure(error, now)
    streak = state.consecutive_fetch_failures
    detail = f"GIS fetch failed ({streak} in a row since {state.first_failure_at:%Y-%m-%d %H:%M} UTC): {error}"
    if streak >= FAILURE_ALERT_THRESHOLD:
        report.errors.append(detail)
    else:
        report.notes.append(
            f"{detail} — not reporting as an error until {FAILURE_ALERT_THRESHOLD} in a row"
        )


def _create_gis_submission(pool, attrs, differences, Submission):
    summary = "; ".join(_describe(f, getattr(pool, f), v) for f, v in differences)
    fields = {f"parsed_{f}": v for f, v in differences}
    Submission.objects.create(
        url=DATASET_PAGE_URL,
        submitter_note=(
            "Auto-detected: city GIS record differs from our data — "
            f"{summary}. Source: OpenDataPhilly PPR Swimming Pools layer."
        ),
        raw_fetched_content=record_snapshot(attrs),
        parsed_pool=pool,
        **fields,
    )
