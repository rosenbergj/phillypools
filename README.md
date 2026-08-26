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

**Fetch failures are deliberately quiet.** ArcGIS Online intermittently answers a
perfectly good request with HTTP 200 whose *body* is an error —
`{'code': 400, 'message': 'Invalid URL', 'details': ['Invalid URL']}` is the one seen.
The URL it requests is a module constant, so it can't be malformed on our side, and a
retry seconds later works. Two things keep that off email: `_get_json` retries each
request (2s then 5s), and `GisCheckState` counts consecutive failed check runs so that
a failure is only reported as an *error* — the thing the digest emails on — once
`FAILURE_ALERT_THRESHOLD` (4) runs in a row have failed, which on the five-a-day cron
is most of a day of a dead source. Below that it's a `note`: visible in the Railway
cron log, silent in your inbox. The first success clears the streak.

To check the streak by hand, look at **GIS check state** in the admin. To check the
endpoint, re-request it a few times:

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  "https://services.arcgis.com/fLeGjb7u4uXqeF9q/arcgis/rest/services/PPR_Swimming_Pools/FeatureServer/0/query?where=1%3D1&outFields=pool_name&returnGeometry=false&f=json"
```

If that comes back fine, the error was transient and there's nothing to fix. A genuine
outage still announces itself: past the threshold every run reports an error, and the
digest rate-limits error mail to one per 24 hours, so a persistently broken source
emails once a day rather than going quiet.

### How we identify ourselves

Every outbound request carries a User-Agent from `pools/services/user_agents.py`,
which is the only place these strings are written. Three tokens, because the honest
answer differs by call:

| Token | Sent by | What it is |
|---|---|---|
| `PhillyPoolsBot/1.0` | `page_monitor`, `gis` | The cron. Fetches a fixed list of pages, follows no links. |
| `PhillyPools-Submission/1.0` | `url_fetcher` | One URL a visitor pasted seconds ago. Not a crawler. |
| `PhillyPools-Admin/1.0` | `scrape_pools`, `rescrape_inactive`, `import_ppr_addresses`, `populate_phillypublicpools_urls` | Hand-run backfills. |

The geocoder is deliberately different — Nominatim's usage policy wants an
application name and a contact address, not a browser string — and the Turnstile
verify POST sends nothing, being a server-to-server API call with a secret rather
than a page fetch someone might want to attribute.

All of them end in `+https://phillypools.app/bot/`, a page explaining who we are and
how to make us stop. **That page has to exist in both serving modes**: a Django route
in `pools/urls.py` and `bot/index.html` from `render_static_site`, sharing one body
template. A test asserts the advertised URL returns 200, because every request we
make points strangers at it.

Keeping the `Mozilla/5.0 (compatible; ...)` prefix is deliberate — Googlebot and
bingbot both send it, and www.phila.gov's WAF answers a bare `python-requests` UA
with **403**. The identification is in the token and the URL, not in pretending to
be a browser. Note that the info URL contains "bot", so a naive `/bot/i` blocklist
matches all of our agents, `-Submission` included; that's accepted rather than worked
around.

Another test walks the AST of every module under `pools/` and fails on any
`requests.get`/`post` with no `headers=` (the Turnstile call is exempted by URL), so a
new call site can't quietly go out as `python-requests/2.x`.

Not done yet: we don't read `robots.txt`. The /bot page says so plainly, and says a
request by hand is what stops us in the meantime.

### Conditional requests on monitored pages

`page_monitor` sends `If-None-Match`/`If-Modified-Since` from the validators stored on
`MonitoredPage`, so an unchanged phila.gov page answers **304 with an empty body**
rather than ~70KB, five times a day, three pages. Verified live: their ETag is stable
across fetches, a bogus one correctly gets a 200, and the validator comes back *weak*
(`W/"..."`) because `requests` asks for gzip — weak is correct here, since it asserts
equivalence of content rather than of octets.

Trusting someone else's validator is the whole risk, so it's bounded three ways:

- **A 304 only ever moves `last_checked`.** Never `content_hash`, never `last_changed`,
  and crucially never `last_full_fetch` — if it did, a server answering 304 forever
  would push the forced-refetch deadline forever and the floor below would never fire.
- **`FULL_FETCH_MAX_AGE` (23 hours)** — if the last real body is older than that, we
  send no validators and demand the page. So a lying ETag, or a CDN edge answering 304
  from a stale copy, can hide a change for at most a day. 23 rather than 24 so the same
  daily cron slot always trips it, instead of the forced fetch drifting an hour later
  each day.
- **`EXTRACTOR_VERSION`** — stored per row, and a mismatch forces a full fetch. Without
  it, improving `_extract_content` would silently never run against pages that haven't
  changed: the page is identical, but what we'd read out of it isn't. **Bump it whenever
  you change how a page body is read.**

The cron log distinguishes `Not modified (304)` from `No change (full fetch)`, so the
two are visible as different events.

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
"show me pools with a lift", but it isn't hidden either — unfiltered views carry a gray
"ADA Lift broken" badge, and the map popup spells the word out rather than showing the
bare ♿, which on its own reads as an assurance.

The layer also carries `ada_access`, `ada_restrooms` and `gender_neutral_restrooms`.
Those are deliberately unused: `ada_lift` is populated on 63 of 64 active records, the
others on far fewer.

**Closing dates:** the layer has no closing-date field today. If the city adds one, map
it in `DATE_FIELD_MAP` (`pools/services/gis.py`) and detection, submissions, and review
all start handling it with no other change. The check watches for a close/end-date field
appearing and reports it, so nobody has to notice by hand.

## How submissions are parsed

Six calls to Claude live in `pools/services/llm_parser.py`. They do not all use the
same model, on purpose:

| Call | Model | Why |
| --- | --- | --- |
| `parse_submission` (URL/text) | Sonnet 5 | Carries the full `_SCHEDULE_INSTRUCTIONS` — time-block merging, en-dashes, per-day annotation. Haiku couldn't hold all of it. |
| `parse_image_submission` | Sonnet 5 | Same instructions, plus OCR. |
| `parse_all_pools_image` | Sonnet 5 | Has to OCR before it can extract. |
| `parse_all_pools` (text) | Haiku 4.5 | Measured identical on the real source — same 62 pools, same dates — at half the latency, and this one blocks an admin request. |
| `parse_heat_emergency` | Haiku 4.5 | Date extraction from a press release. |
| `moderate_image` | Haiku 4.5 | A 64-token yes/no. |

**Never read `message.content[0].text`.** Sonnet 5 thinks adaptively whenever the
`thinking` parameter is omitted, so the response may lead with a `ThinkingBlock` and
`content[0]` is not reliably the answer. Because that decision is made per request, the
same call succeeds and fails at random — roughly half of image submissions were failing
with `'ThinkingBlock' object has no attribute 'text'` before anyone noticed a pattern.
Use `_response_text()`, which joins the text blocks and skips the rest.

Thinking tokens also count against `max_tokens`, which is why the sonnet calls ask for
8192–16000 rather than the 1024 that was enough before. Two things go wrong when that
budget is too small: the JSON is truncated mid-object, or the response carries no text at
all. `_no_text_note()` records `stop_reason` and the block types in that second case, so
an exhausted budget doesn't reach the admin as a blank field.

### Truncation is silent, so the caps have to agree

`fetch_url` truncates before any parser sees the text, and its default (8000) is smaller
than what `parse_all_pools` works from. The call site passes `ALL_POOLS_MAX_CHARS`
explicitly for that reason — leave it off and the fetcher's default quietly wins, dropping
pools off the end of a long page with no error and nothing in the admin to show it
happened.

### Form submissions are parsed in the background

The submit view saves the row and returns, then a daemon thread runs the LLM. So a
submission opened straight away exists but has no results yet. Two consequences:

- **The background save names its columns** (`update_fields=[...]`). A bare `save()`
  writes back every field from the copy loaded before the LLM call, which silently
  reverted `status`, `moderator_notes` and `reviewed_at` if the submission was reviewed
  while its parse was still running.
- **`Submission.llm_parse_pending`** is null `llm_response` within a two-minute grace
  period, and the admin change form shows a banner and a `<meta http-equiv="refresh">`
  while it holds. Emitting the refresh only in that state makes it self-terminating. The
  grace period matters because the thread is a daemon in a gunicorn worker: a restart
  mid-parse leaves `llm_response` null forever, and without the bound that page would
  reload on it indefinitely.

`parse_all_pools` and `parse_all_pools_image` are the exception — they run inline in the
admin's "Re-parse for all pools" request, which is why gunicorn runs with `--timeout 120`
(the real page takes ~18s, and would have exceeded the 30s default at sonnet's ~32s).

## How long a pool was open

"Days open" means **opening day to closing day, counting both endpoints** —
`(closing_date - opening_date).days + 1`. A pool open June 17 through June 17 was open
one day, not zero.

That definition lives once, in `season_length_days` (`pools/services/season.py`), because
two things quote it and must not disagree: the duration on each archived pool page
("7 weeks, 3 days") and the season-length histogram on the offseason index. Both read
their dates from `season_snapshot` in the same module, which prefers `PoolSeasonHistory`
and falls back to the pool's live fields.

A pool missing either date has a length of `None`, never `0`. "We don't know" and "was
open no days" are different claims about a real pool, and only the first is one we can
make — so such a pool is excluded from the histogram and from the longest/shortest
bullets, while still counting toward "N pools opened" if it has an opening date.

The summary bullets and the histogram are generated at shutdown by `render_static_site`,
not by a live view; see `offseason-runbook.md` step 3, including how to preview the build
from another machine while tweaking it.
