# Phillypools

[phillypools.app](https://phillypools.app)

## Where pool data comes from

Three sources, none of them complete on their own. The app exists because this
information isn't centralized anywhere else.

| Source | Carries | Caveat |
| --- | --- | --- |
| **phila.gov press releases** (opening schedule, closings schedule) | Opening and closing dates | Published a few times a season, then frozen. Pools get left off: the 2026 closings release omitted Baker and Ford entirely. |
| **City ArcGIS layer** (`PPR_Swimming_Pools`, via OpenDataPhilly) | Pool inventory, address, coordinates, `pool_status`, `pool_open_date`, `ada_lift` | No closing date at all. Keeps being edited after the press releases freeze. |
| **Submissions** (public form, plus monitored pages) | Anything a person can see — hours, signage, closures | Uneven per pool, and depends on who happens to be posting. |

### The GIS check

`check_pool_gis` reads the ArcGIS **FeatureServer query API** — deliberately not the
Hub GeoJSON *download* endpoint that `scrape_pools` and `rescrape_inactive` use, since
that one serves a periodically-regenerated export and a change detector wants the live
table.

When a GIS date disagrees with what a pool holds, it files a normal `Submission` for
review, so GIS proposals land in the same admin queue and digest as everything else.
Approving one applies it through the usual `apply_to_pool` path and cites the
OpenDataPhilly dataset page as the source.

It proposes a given value **once**. `PoolGisState` remembers what was last put to
review, so rejecting a GIS date doesn't cause it to be re-proposed on the next cron
run — a new proposal only follows an actual change in GIS. To deliberately re-offer a
value you previously rejected, clear that pool's `proposed_*` field in the admin.

Why this is worth having: Amos Pool reopened 7/31/2026 after a rebuild. That reached
the GIS layer but never reached the opening-schedule page, which had frozen on July 1.

Run `check_pool_gis --dry-run` to preview proposals without writing anything. It runs
on the same cron as the other checks, via `run_url_watcher`.

### Accessibility: `Pool.ada_lift`

Three states — `yes`, `none`, `broken` — rather than a boolean, because a lift that
exists but doesn't work is neither of the other two.

`scrape_pools` syncs it from the feed's `ada_lift` column: an explicit `Y` becomes
`yes`, anything else (including blank) becomes `none`. The city has no way to say
"broken", so:

- the scraper **never sets** `broken` — it's entered by hand in the admin;
- a feed `Y` **never overwrites** an existing `broken`, or the next sync would silently
  erase the one fact here the city doesn't have;
- a feed `N` **does** overwrite it, because "there is no lift" makes `broken` stale
  rather than better-informed.

The `?ada_lift=1` filter matches **working lifts only**. A broken lift can't answer
"show me pools with a lift", but it isn't hidden either — unfiltered views carry a grey
"ADA Lift broken" badge, and the map popup spells the word out rather than showing the
bare ♿, which on its own reads as an assurance.

The layer also carries `ada_access`, `ada_restrooms` and `gender_neutral_restrooms`.
Those are deliberately unused: `ada_lift` is populated on 63 of 64 active records, the
others on far fewer.

**Closing dates:** the layer has no closing-date field today. If the city adds one, map
it in `DATE_FIELD_MAP` (`pools/services/gis.py`) and detection, submissions, and review
all start handling it with no other change. The check watches for a close/end-date field
appearing and reports it, so nobody has to notice by hand.
