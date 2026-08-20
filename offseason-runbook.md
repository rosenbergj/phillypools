# Offseason Runbook

Two halves: **shutting down at end of season** and **rebuilding at start of next season.**

---

## Part 1 — End of Season Shutdown

Do these steps in order. Two ordering constraints drive the sequence: the static
site has to be rendered while this season's data is still live and un-reset, and
everything has to be backed up before the Railway project is deleted.

### 1. Pause the cron service

In the Railway dashboard, disable the cron on the `url-watcher` service before
anything else. It runs five times a day, and any submission it creates after you
clear the queue (step 2) or take the backup (step 6) is work you'll either lose or
have to redo. Nothing else in this runbook is safe to do while it's still firing.

### 2. Clear the submission queue

Review and resolve all pending submissions in the admin (`/admin/pools/submission/`).
Approved submissions should already be applied to pool records. Reject or delete
anything leftover so you start the next season clean.

### 3. Render the static offseason site

```bash
python manage.py render_static_site --season-year 2026
```

This writes `offseason-build/` — an archived page for every pool at its real URL
(`/pools/<slug>/`), plus the index, `sitemap.xml`, `robots.txt`, a `404.html`, and a
`favicon.ico` built from `offseason/favicon.png`.

It also draws the **season-length histogram** on the index page: how many days each pool
was open, counted from opening day to closing day inclusive, bucketed 2 days to a bar.
This is why the command runs at shutdown and not earlier — the chart is only honest once
every pool has actually closed and its closing date is final. The output line tells you
what it drew, and that number is worth reading before you deploy:

```
Rendered histogram of 63 season length(s), 39–88 days, median 57
```

If the count is well below the number of pools that opened, some closing dates never
landed — check those before deploying rather than publishing a chart that quietly omits
them. Pools with a missing date aren't guessed at or counted as zero; they're excluded
and disclosed in the caption under the chart ("Based on the 63 pools with both an opening
and a closing date on record; N others…"). A season with no dates at all draws no chart
at all, rather than empty axes.

Bucket size is `--histogram-bin-width` (default 2). Widen it if a season's dates come out
spikier than usual; it's there so you can re-render without editing code. The definition
of "days open" lives in `pools/services/season.py` and is shared with the duration text on
each pool page ("7 weeks, 3 days"), so the chart and the pages can't drift apart.

Inactive pools are included, in the pages, the index, and the sitemap. `is_active` only
means we don't expect an opening date; the pool still exists and its page still carries
an address, notes, and last season's history. A pool that never opened is labeled "Did
not open" in the past tense and skips the typical-citywide-hours copy, which would
otherwise imply it kept those hours.

**Why this exists:** the alternative — redirecting every pool URL to a single
"see you in the spring" page — quietly costs the site its search presence every
winter. Google treats a redirect that persists for months as permanent, and pointing
~70 distinct URLs at one unrelated page is the textbook soft-404 pattern, so the pool
pages drop out of the index and have to re-earn their rankings each May. Serving real
archived content at the same URLs keeps them indexed and actually answers what a
March searcher is asking. This is also why `offseason/_redirects` must never contain
a `/pools/*` rule: Cloudflare Pages evaluates redirects before static assets, so a
catch-all there would shadow every page this command generates.

Three things to know:

- **It must run locally, not via `railway ssh`.** It writes files to disk, and a
  Railway container's disk is ephemeral. Get prod data onto your machine first by
  following `sync-prod-db.md`, then run the command against the local DB.
- **Run it before `reset_season`, never after.** It reads `PoolSeasonHistory` for the
  season year and falls back to the pool's live fields, so it needs this season's data
  still in place. `reset_season` stays a *start-of-next-season* step (`season-setup.md`)
  precisely so it can't run first.
- **`--season-year` defaults to the current calendar year**, which is right for a fall
  shutdown. Pass it explicitly if you're doing this after January 1.

The output reports how many pools had no schedule for the season and so use the
generic-hours copy (`-v 2` lists them). Expect roughly half — PPR doesn't publish hours
for many pools and for some they aren't online anywhere, so this is the normal state and
not something to chase before deploying. Then preview the whole thing locally:

```bash
python -m http.server -d offseason-build
```

Display images are deliberately omitted from these pages. R2 URLs are presigned and
expire in an hour (`AWS_QUERYSTRING_AUTH` is unset, so django-storages defaults it on),
so baking them into HTML served for eight months would produce broken images. They're
also mostly proof/source shots for a current schedule, which isn't worth much once the
schedule is archival.

### 4. Delete the pool-info monitored pages

Visit `/admin/pools/monitoredpage/` and delete the pages with type **"Pool info"**.
Those URLs change year to year (PPR reorganizes their site, pool social links move),
so it's cleaner to start fresh next season than to restore stale ones.

**Leave the "Heat emergency info" page(s) alone.** The DPH press-release index is stable
infrastructure, not a season-specific URL. It was originally seeded by migration `0020`,
but that migration is already recorded in `django_migrations` — which is inside the
backup — so it will *not* re-run on a restore. Delete it now and next season starts with
no heat-emergency page at all.

Do this before the database dump so the stale pool-info pages don't end up in the backup.

### 5. Aggregate and clear raw usage data

The `/stats/` page is backed by two tables: `UsageEvent` (one row per interaction,
carrying a daily-rotating visitor pseudonym) and `UsageDaily` (permanent counts, no
pseudonyms). Roll everything up and drop the raw rows *before* the backup, so the
`.dump` you keep long-term contains counts only:

```bash
railway ssh --service web -- python manage.py rollup_usage --all
railway ssh --service web -- python manage.py shell -c \
  "from pools.models import UsageEvent, VisitorSalt; UsageEvent.objects.all().delete(); VisitorSalt.objects.all().delete()"
```

`rollup_usage --all` aggregates every day that still has raw rows, so nothing is
lost by deleting them. `UsageDaily` is what makes year-over-year comparison
possible next season — it must stay.

### 6. Back up the database

From your local machine with the Railway CLI installed:

```bash
railway link          # select the phillypools project
railway connect Postgres
```

Or get the connection string from the Railway dashboard (Postgres service → Variables → `DATABASE_URL`) and run:

```bash
pg_dump "<DATABASE_URL>" -Fc -f phillypools-$(date +%Y%m%d).dump
```

Keep this `.dump` file somewhere safe — it's a full `pg_dump` of every table (pool data, season history, submission history, site announcements, etc.), so nothing needs separate handling. A password manager attachment or personal cloud storage works fine.

### 7. Verify the backup

Do this before step 11, not after. Once the Railway project is gone this dump is the
only copy of the data, and a dump you've never read back is not a backup.

```bash
pg_restore --list phillypools-YYYYMMDD.dump | head -40
```

That confirms the file isn't truncated and lists the tables it contains — check that
`pools_pool`, `pools_poolseasonhistory`, `pools_usagedaily`, and `pools_submission` are
all in there. If you have a local Postgres handy, a full restore into a scratch database
is a stronger check and takes another minute.

### 8. Record all environment variables

From the Railway dashboard, copy every env var from both the **web service** and the **cron service** to your password manager or a secure note. The ones that matter and won't be auto-regenerated:

- `SECRET_KEY`
- `ANTHROPIC_API_KEY`
- `CLOUDFLARE_TURNSTILE_SITE_KEY`
- `CLOUDFLARE_TURNSTILE_SECRET_KEY`
- `R2_ACCOUNT_ID`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_BUCKET_NAME`
- `SES_ACCESS_KEY_ID` (cron service)
- `SES_SECRET_ACCESS_KEY` (cron service)
- `SES_REGION` (cron service)
- `DIGEST_FROM_EMAIL` (cron service)
- `DIGEST_TO_EMAIL` (cron service)

`DATABASE_URL` and `RAILWAY_PUBLIC_DOMAIN` will be different on the new project — don't bother saving them.

> **Note:** R2 media uploads (pool photos, submission images) live in Cloudflare R2 independently of Railway and don't need to be backed up — they'll still be there next season.

### 9. Deploy the offseason site to Cloudflare Pages

Deploy the `offseason-build/` directory you rendered in step 3:

```bash
wrangler pages deploy offseason-build
```

If the Cloudflare Pages project doesn't exist yet, create it first (Cloudflare
dashboard → Pages), and add both `phillypools.app` and `www.phillypools.app` as custom
domains on it.

Note that the deploy target is `offseason-build/` (generated, gitignored), not
`offseason/` (the hand-maintained assets — `pic.png`, `favicon.png`, `og-preview.png`,
`_redirects` — which `render_static_site` copies into the build). Editing the greeting
or the pool list means editing `pools/templates/pools/offseason_index.html` and
re-rendering, not editing built HTML.

Preview via the Cloudflare Pages URL before switching DNS in the next step. Spot-check a
few pool pages, `/sitemap.xml`, `/robots.txt`, and `/favicon.ico` — Google's favicon
crawler goes to that last path, and the live site answers it from a Django view that
isn't there once the site is static, so the build generates the file instead.

### 10. Switch DNS to Cloudflare Pages

DNS for `phillypools.app` is at **Namecheap**, not Cloudflare. Update both the
`phillypools.app` and `www.phillypools.app` records to point at the Cloudflare Pages
URL instead of the Railway-provided domain. Verify the offseason site loads on both.

### 11. Delete the Railway project

Once DNS has propagated and you've confirmed the offseason site is live, delete the Railway project from the Railway dashboard. This removes all three services (web, Postgres, cron) and stops billing.

### 12. Resubmit the sitemap in Search Console

The sitemap URL is unchanged (`https://phillypools.app/sitemap.xml`) and now lists the
homepage plus every archived pool page, so there's nothing to reconfigure — but ask
Search Console to re-fetch it so the offseason set is picked up promptly. Over the next
few weeks, check Coverage: the pool URLs should stay indexed. If they start showing up
as "Page with redirect" or "Soft 404," something is shadowing the static pages — check
`_redirects` first.

---

## Part 2 — Start of Season Rebuild

Do these roughly in order. The database restore comes *before* the first web deploy,
and the DNS cutover is last — the app can run on a Railway URL for testing before you
point the real domain at it.

### 1. Create a new Railway project

In the Railway dashboard, create a new project named `phillypools` (or similar).

### 2. Add a PostgreSQL service

Add the Railway Postgres plugin/service. Once provisioned, Railway will inject `DATABASE_URL` into services in the same project automatically.

### 3. Restore the database

Do this **before** deploying the web service. `railway.toml`'s start command runs
`migrate --noinput` on every web deploy, so deploying first would create a full empty
schema and then `pg_restore` would collide with it — duplicate keys on
`django_migrations`, failed `CREATE TABLE`s, and a half-restored database. Worse, the
healthcheck (`healthcheckPath = "/"`) passes fine against an empty DB, so the deploy
goes green and the problem isn't obvious.

Get the new `DATABASE_URL` from the Railway Postgres service variables, then restore:

```bash
pg_restore -d "<NEW_DATABASE_URL>" --no-owner phillypools-YYYYMMDD.dump
```

This restores every table from the dump — pool records, season history, previous
submissions, site announcements, etc.

Last season's `/stats/` history comes back with it, so year-over-year comparison
works immediately. Nothing needs configuring: `UsageEvent` starts empty (you cleared
it in Part 1), the nightly visitor salt regenerates itself on the first request, and
the rollup runs off the existing cron service rather than a schedule of its own.
Note that per-pool history is keyed by slug, so a pool renamed between seasons shows
its old slug for last season's rows and its new name for this season's.

### 4. Deploy the web service

Add a new service from the GitHub repo (`phillypools`). Railway will use `railway.toml` for build and start commands automatically.

Set the following environment variables on the web service:

```
SECRET_KEY=<from your saved backup>
ANTHROPIC_API_KEY=<from your saved backup>
CLOUDFLARE_TURNSTILE_SITE_KEY=<from your saved backup>
CLOUDFLARE_TURNSTILE_SECRET_KEY=<from your saved backup>
R2_ACCOUNT_ID=<from your saved backup>
R2_ACCESS_KEY_ID=<from your saved backup>
R2_SECRET_ACCESS_KEY=<from your saved backup>
R2_BUCKET_NAME=<from your saved backup>
DATABASE_URL=${{Postgres.DATABASE_URL}}
ALLOWED_HOSTS=*
```

Use Railway's cross-service reference syntax (`${{ServiceName.VAR_NAME}}`) for `DATABASE_URL` rather than a hardcoded connection string — this way it stays correct if Railway rotates credentials. At the time this runbook was written, `DATABASE_URL` was the only cross-service reference on the web service, but check your saved env var notes to confirm nothing else was added since.

`RAILWAY_PUBLIC_DOMAIN` is injected automatically — don't set it manually.

The deploy's `migrate` now runs on top of the restored schema, which is exactly what you
want: it applies any migrations that were merged over the offseason and brings last
season's dump up to current code. Watch the deploy log to confirm those migrations
applied cleanly.

Verify by visiting the Railway-provided URL for the web service and checking the admin.

> **Testing gotcha:** Turnstile keys are bound to specific hostnames, and the
> Railway-provided domain isn't one of them. `/submit/` will fail its captcha on that
> temporary URL — that's expected, not a bug. Test submissions after the DNS cutover in
> step 7, or temporarily add the Railway hostname in the Cloudflare Turnstile dashboard.

### 5. Add the cron service

Add a second service from the same GitHub repo. All of its variables go on the cron service itself (Variables tab of that service). Three are cross-service references to the web service (replace `phillypools` with whatever the web service is actually named in the Railway dashboard); the five SES digest-email vars are **plain literal values** from your saved backup — they exist only on the cron service and are not set on (or referenced from) the web service, since only the cron sends email:

```
RAILWAY_SERVICE_NAME=url-watcher
ANTHROPIC_API_KEY=${{phillypools.ANTHROPIC_API_KEY}}
DATABASE_URL=${{phillypools.DATABASE_URL}}
SECRET_KEY=${{phillypools.SECRET_KEY}}
SES_ACCESS_KEY_ID=<literal value from your saved backup>
SES_SECRET_ACCESS_KEY=<literal value from your saved backup>
SES_REGION=<literal value from your saved backup>
DIGEST_FROM_EMAIL=<literal value from your saved backup>
DIGEST_TO_EMAIL=<literal value from your saved backup>
```

In the Railway dashboard, configure this service as a **Cron** with schedule `15 1,13,16,19,22 * * *` (runs at 1:15 AM and 1:15, 4:15, 7:15, and 10:15 PM UTC — the 1:15 AM UTC run is the 9:15 PM EDT evening check). The `railway.toml` start command already branches on `RAILWAY_SERVICE_NAME`, so this service will run `run_url_watcher` when triggered — that runs the page and heat-emergency checks, then emails a digest if anything new is pending (see `pools/services/digest.py`). The SES sender/recipient identities live in AWS SES (sandbox mode is fine — both addresses just need to be verified there) and survive the offseason teardown.

> **Naming gotcha:** Railway injects `RAILWAY_SERVICE_NAME` itself, from the service's
> actual name, so setting it by hand relies on the manual value winning over the
> platform's. The reliable move is to *name the service* `url-watcher` in the dashboard,
> which makes the manual variable redundant. Confirm the first run actually executes
> `run_url_watcher` and not gunicorn.

### 6. Run the new season setup

Follow `season-setup.md` from the beginning.

### 7. Switch DNS back to Railway

Add both `phillypools.app` and `www.phillypools.app` as custom domains on the Railway web service (Railway dashboard → web service → Settings → Custom Domains). Update both DNS records (at Namecheap) to point to the Railway-provided CNAMEs. Verify the live app loads correctly on both.

### 8. Set up monitored pages

Visit `/admin/pools/monitoredpage/`. The heat-emergency page(s) should have come back
with the restore (Part 1 step 4 deliberately left them alone) — confirm at least one is
present, because `check_heat_emergency` reports an error into the digest email rather
than silently checking nothing if none exists. If it's missing, re-add the DPH
press-release index with page type "Heat emergency info".

Then add this season's **"Pool info"** pages, which you deleted at shutdown. The cron
will establish a baseline content hash on its first run — no submission is created on
that first check.

### 9. Re-verify Search Console

Confirm the sitemap still fetches cleanly now that it's served by Django again, and
request indexing for the homepage and a couple of pool pages so the live (non-archived)
content gets picked up quickly. The URL set is unchanged from the offseason build, so
there's nothing to remove or redirect.
