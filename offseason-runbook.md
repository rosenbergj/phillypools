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
clear the queue (step 2) or before you take the backup (step 6) is work you'll
either have to redo or lose outright. Those two steps are the only ones that
actually depend on it being off — steps 3-5 don't touch submissions or usage data
either way — but a real shutdown doesn't happen in one sitting: DNS propagation,
scheduled deletions, and just working through the list take hours to days, not
minutes (this one did). Pause it first so that gap can't accumulate several more
cron cycles' worth of submissions before you get back to the backup, rather than
timing the pause around exactly when it's needed.

If there's no direct disable toggle in the dashboard, changing the cron schedule to a
day that won't occur before step 11 deletes the project (e.g. `15 1,13,16,19,22 6 * *`,
day-of-month 6) is an effective substitute — confirmed working 2026-09-07. The schedule
lives in Railway's dashboard, not in `railway.toml`, so there's nothing to revert once
the project is deleted.

### 2. Clear the submission queue

Review and resolve all pending submissions in the admin (`/admin/pools/submission/`).
Approved submissions should already be applied to pool records. Reject or delete
anything leftover so you start the next season clean.

### 3. Render the static offseason site

**Sync the prod database first** (`sync-prod-db.md`) — this command must run locally
(see below), against local data, and only prod has this season's final numbers.
Re-sync any time you suspect prod has moved since your last one.

```bash
python manage.py render_static_site --season-year 2026
```

This writes `offseason-build/` — an archived page for every pool at its real URL
(`/pools/<slug>/`), plus the index, `sitemap.xml`, `robots.txt`, a `404.html`, a
`favicon.ico` built from `offseason/favicon.png`, and `bot/index.html`.

That last one matters more in the offseason than in season: every request the app
makes carries `+https://phillypools.app/bot/` in its User-Agent, so a sysadmin who
got annoyed in August may well go looking in February, when there's no Django left
to answer. It's rendered from the same body template as the live page.

It also builds the index page's **season summary** — a short bullet list (how many pools
opened, the longest and shortest seasons, the median season length labeled "Median pool
season," the earliest opening and the latest closing, each naming the pools involved where
one applies — the median only names pools when it lands exactly on a real length, which an
even pool count and no tie at the middle can leave without one) and, below it, the
**season-length histogram**: how many
days each pool was open, counted from opening day to closing day inclusive, bucketed 2
days to a bar.

This is why the command runs at shutdown and not earlier — both are only honest once every
pool has actually closed and its closing date is final. The output lines tell you what
they covered, and those numbers are worth reading before you deploy:

```
Summarized 64 pool opening(s)
Rendered histogram of 63 season length(s), 39–88 days, median 57
```

Every pool tied for an extreme is named, so "Earliest pool opening: June 12 (Hunting Park
Pool, Kelly Pool)" is the normal shape of those bullets — the city opens in waves and ties
are common. A pool that opened but has no closing date still counts toward the opened
total; it just can't win longest, shortest, or latest closing.

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

#### Iterating on the layout

Expect to re-render several times on switchover day, tweaking the index page. Set up for
that once and the loop is a couple of seconds:

```bash
# terminal 1 — leave this running all day
python -m http.server 8800 -d offseason-build

# terminal 2 — after each edit
python manage.py render_static_site --season-year 2026
```

Then reload the browser. A full render is ~0.4s for all ~70 pages and touches nothing but
the local database, so there's no reason to render a subset. The server keeps working
across re-renders even though the command deletes and recreates `offseason-build/`, so you
don't need to restart it.

**Viewing it from the laptop.** This box is headless, so just browse to it over the LAN:
**<http://192.168.1.116:8800/>**. Verified working 2026-08-20. `http.server` binds
`0.0.0.0`, and no firewall is running here (ufw, firewalld, nftables and
netfilter-persistent are all inactive), so there's nothing to open and no tunnel needed.

Worth knowing if it ever doesn't answer: `enp3s0` carries **both** `192.168.1.116` and
`192.168.0.116` on a /22, so the LAN spans `192.168.0.0`–`192.168.3.255` and the laptop
usually sits on `192.168.0.x` while this box answers on `192.168.1.x`. Either address
reaches it. Check the address you typed before assuming a network problem — and the
server's own log is the fastest way to tell a dropped packet from a typo, since a request
that arrives shows up there with the client IP.

If you ever do need to get in without the port being reachable, an SSH tunnel needs
nothing configured:

```bash
ssh -N -L 8800:localhost:8800 josh@192.168.1.116   # then http://localhost:8800/
```

8800 is used here because 8080 is already taken on this box and 8000 is where `runserver`
lands, so this port won't collide with the live site if you're running both to compare.
Nothing is published by any of this — `render_static_site` only writes a local directory,
and the site goes live only at the `wrangler pages deploy` in step 9.

Two things that matter here:

- **Sync the prod database first** (`sync-prod-db.md`), once, before you start iterating.
  Everything after that is local and offline. A render against a stale local database will
  produce a plausible-looking page with the wrong numbers — check the summary line against
  what you expect before believing the page.
- **Edit the templates, never the built HTML.** `offseason-build/` is deleted and rebuilt
  on every run, so any hand-edit there is gone at the next render. The index — greeting,
  bullets, chart, pool list — is all in `pools/templates/pools/offseason_index.html`.

Only deploy once you're happy; the `wrangler pages deploy` in step 9 is a separate step
precisely so you can iterate freely here without publishing each attempt.

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

**Retired numeric URLs.** The command also appends an exact-match
`/pools/<id>/ -> /pools/<slug>/` 301 for every pool to its copy of `_redirects`
(two lines each, for the trailing-slash and bare forms). Live, those numeric URLs
are a 301 from `pool_detail_pk_redirect`; nothing on the site links to them any
more, but search engines still hold old results that do — Google crawled
`/pools/15` on 2026-09-07 — and a URL that 301s in season, 404s all winter, then
301s again in May is worse off than one that redirects consistently. These are
exact matches, not a splat, so they can't shadow the archived pages, and a slug
starting with a digit (`12th-and-cambria-pool`) is never a purely numeric path.

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

**This makes step 3 a one-shot for the /bot page.** Its "a fixed, hand-picked list of N
pages" line is counted from this table, so any later re-render from the backup (to fix
pool text mid-offseason, say) counts only the heat-emergency page and publishes "1 page"
instead of the in-season number. Found on a 2026-09-16 re-render. Until the renderer
stops depending on it, keep the previously deployed `bot/index.html` when re-rendering
— copy the old build aside first and put that one file back before deploying.

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

From your local machine with the Railway CLI and `pg_dump` installed:

```bash
railway link          # select the phillypools project

# terminal 1 — leave this running
railway connect Postgres --tunnel-only

# terminal 2 — use the localhost URL the command above printed
pg_dump "<printed-local-url>" -Fc -f offseason-backups/phillypools-$(date +%Y%m%d).dump
```

**Don't copy `DATABASE_URL` straight from the Railway dashboard into `pg_dump`** — it
ends in `@postgres.railway.internal:5432/railway`, a hostname that only resolves inside
Railway's private network. It won't connect from this box or your laptop, and this
project has no public `DATABASE_PUBLIC_URL` to fall back to (same issue `sync-prod-db.md`
works around with `railway ssh` instead of a direct connection). `railway connect
Postgres --tunnel-only` is what actually bridges that gap: it holds open a local tunnel
and prints a `localhost:<port>` connection string `pg_dump` can use. (Plain `railway
connect Postgres`, no flag, also works, but only for an interactive `psql` shell —
`--tunnel-only` is what's needed to point `pg_dump` at it.) This is based on reading
`railway connect --help`'s documented behavior, not a completed dump — if it doesn't
work as described, update this note with what actually happened.

`offseason-backups/` is gitignored and lives on this dev box — it's picked up by the box's own
server-wide backups, so nothing further needs doing here. It's a full `pg_dump` of every
table (pool data, season history, submission history, site announcements, etc.), so
nothing needs separate handling.

### 7. Verify the backup

Do this before step 11, not after. Once the Railway project is gone this dump is the
only copy of the data, and a dump you've never read back is not a backup.

```bash
pg_restore --list offseason-backups/phillypools-YYYYMMDD.dump | head -40
```

That confirms the file isn't truncated and lists the tables it contains — check that
`pools_pool`, `pools_poolseasonhistory`, `pools_usagedaily`, and `pools_submission` are
all in there. If you have a local Postgres handy, a full restore into a scratch database
is a stronger check and takes another minute.

### 8. Record all environment variables

Dump both services straight to file rather than copying values by hand from the
dashboard — faster, and nothing gets fat-fingered:

```bash
cd offseason-backups
(umask 077 && railway variable list --service web --kv > env-vars-web-<year>.txt)
(umask 077 && railway variable list --service url-watcher --kv > env-vars-cron-<year>.txt)
```

**Redirect straight to a file every time — never run `railway variable list` bare.**
Its default table output includes full raw secret values, not just names, despite
looking like a summary; only `--kv`/`--json` are documented as doing that, but the
plain default does too. The `umask 077` subshell matters too — it makes the file mode
600 from the moment it's created, with no window where a default-permission file sits
there briefly readable by anyone else on the box.

Once both files exist, trim each down to what Part 2 steps 4–5 actually need — delete
every `RAILWAY_*` line (Railway injects these itself; they won't match the new
project's IDs anyway), `DATABASE_URL` (new project, new value), and any other
build/platform noise (`DEBUG`, `ALLOWED_HOSTS`, `MISE_PYTHON_GITHUB_ATTESTATIONS` at
last count) — none of it needs restoring. What should survive the trim, the vars that
matter and won't be auto-regenerated:

- `SECRET_KEY`
- `ANTHROPIC_API_KEY`
- `CLOUDFLARE_TURNSTILE_SITE_KEY`
- `CLOUDFLARE_TURNSTILE_SECRET_KEY`
- `CARTO_API_KEY` (web service — without it the map tiles come back stamped "API KEY REQUIRED")
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
few pool pages, `/sitemap.xml`, `/robots.txt`, `/favicon.ico`, and `/bot/`. The last two
are the ones with no Django behind them any more: Google's favicon crawler goes to
`/favicon.ico`, which the live site answers from a view, and `/bot/` is the URL every
User-Agent we send points at all year.

### 10. Switch DNS to Cloudflare Pages

DNS for `phillypools.app` moved from Namecheap to **Cloudflare** during the 2026
shutdown (Cloudflare Pages' custom-domain feature requires the apex to be a zone on
the same account — Namecheap couldn't satisfy that without breaking HTTPS on a plain
redirect). Namecheap is still the registrar; Cloudflare is now authoritative for DNS.
Update both the `phillypools.app` and `www.phillypools.app` records, in Cloudflare's
own DNS dashboard, to point at the Cloudflare Pages URL instead of the Railway-provided
domain. Verify the offseason site loads on both.

**`www` → apex redirect.** In season, Django's `WWWRedirectMiddleware` 301s `www` to the
apex; the static site has no equivalent, and `_redirects` can't do it (it matches paths,
not hosts), so `www` served every page as a 200 duplicate until this was added on
2026-09-16. It's a **Bulk Redirect** in the zone, set up following
<https://developers.cloudflare.com/pages/how-to/www-redirect/>: a list with source
`www.phillypools.app`, target `https://phillypools.app`, status 301, and all four
parameters (preserve query string, subpath matching, preserve path suffix, include
subdomains), plus a bulk redirect rule using that list. **Skip the guide's step 3** (the
dummy `A www 192.0.2.1` record): the existing proxied `www` record already routes traffic
through Cloudflare, which is all the redirect needs, and the `www` custom domain stays
attached to the Pages project. It lives in the zone, not the Pages project, so it should
survive between shutdowns — check it still exists rather than recreating it:

```bash
curl -sI "https://www.phillypools.app/pools/amos-pool/?x=1" | grep -iE "^(HTTP|location)"
# expect 301 and location: https://phillypools.app/pools/amos-pool/?x=1
```

The zone also carries the site's other DNS: 3 SES DKIM CNAMEs, an SPF TXT, Namecheap's
email-forwarding MX records (`eforward*.registrar-servers.com` — a pre-existing feature,
unrelated to this migration), and a `google-site-verification` TXT for Search Console.
None of these need touching here; they were carried over once, during the migration.

### 10.5. Migrate `railway.toml` to Infrastructure as Code

> **This step is for the 2026 shutdown only.** It exists because Railway's Config as
> Code deprecation lands mid-offseason. Once the 2027 rebuild has run successfully on
> `.railway/railway.ts`, **delete this step** — it has no reason to run again.

Railway stops reading `railway.json` / `railway.toml` on **2026-12-01**, a hard cutoff
that falls between this shutdown and next season's rebuild. Part 2 steps 4 and 5 both
assume Railway picks up `railway.toml` automatically, so the rebuild would hit this cold
if it isn't handled now.

**Do it here, in this exact gap, and not earlier or later.** Step 10 already moved
traffic to Cloudflare Pages, so anything breaking in Railway now costs nothing. Step 11
deletes the project, after which there is no longer a configuration to migrate *from*.
This is the only point in the year where both are true — and you have the whole setup
paged in from steps 8-11, rather than meeting it cold next June.

Run `railway config migrate --apply` (the plain form without `--apply` is a dry run —
it only prints what it would write), review the generated `.railway/railway.ts`, and
commit it, along with `package.json`/`package-lock.json` (see below). What it must
preserve:

- the build command (`collectstatic`)
- the healthcheck path and timeout
- **the `RAILWAY_SERVICE_NAME` branch in the start command** — this is the load-bearing
  part, and the reason one repo can serve as both the web service and the cron service.
  If the migration flattens that into a single start command, the rebuild's step 5 stops
  working and you won't find out until June.

**`railway config plan`/`apply` need the `railway` npm package installed at the repo
root** (`npm install railway`) — `.railway/railway.ts` imports `railway/iac`, and unlike
`config migrate` (which only translates the file), `plan`/`apply` actually execute it.
Commit the resulting `package.json`/`package-lock.json`; `node_modules/` is gitignored.

**The question this step exists to de-risk, answered 2026-09-08:** migrating proves IaC
can *describe* a project that already exists; the rebuild needs it to *create* one from
nothing, which is a different capability. Tested against a real throwaway project
(`railway init --name <x>` → `railway config plan` → `railway config apply`, then
`railway delete --project <id> --yes` to clean up — deletion is scheduled by Railway,
not instant, so it may still list briefly afterward):

- **It does create a service from nothing** — `config apply` against an empty project
  correctly planned and applied "1 to add," and `railway status` showed the new service
  with the right build/start/healthcheck config.
- **But it doesn't connect a GitHub source.** The created service showed **Offline**,
  and `railway logs --build`/`railway logs` both reported "No deployments found" — there
  was code nowhere to build. `config apply` only ever creates/updates a service's
  *settings*; attaching it to this repo is a separate action IaC doesn't cover.

So: **Part 2 steps 4 and 5 stay as dashboard-driven "add a service from the GitHub
repo"** — IaC doesn't replace that part. What it *does* replace is manually re-entering
build/start/healthcheck: once each service is connected to the repo, run
`railway config plan` then `railway config apply` against it to make sure those settings
actually land, rather than assuming the dashboard's defaults match `.railway/railway.ts`.

Still unconfirmed, and untested by the above (would need a real repo connection to
check): whether Railway auto-applies a committed `.railway/railway.ts` on deploy the way
it currently auto-reads `railway.toml`, or whether `config apply` always has to be run
by hand. Assume the latter — running `config apply` explicitly after connecting each
service in Part 2 steps 4/5 — until proven otherwise.

### 11. Delete the Railway project

**Optional:** the web service has stayed live and cron paused through steps 6–10, so any
visits since step 5's cleanup exist only as raw, un-aggregated `UsageEvent` rows — nothing
has rolled them into `UsageDaily` since. If you want this last stretch of traffic counted
rather than discarded, repeat step 5's rollup-and-delete and step 6's `pg_dump` once more
right before deleting the project, and use that dump for the restore instead. Skipping this
costs nothing but a little usage data for the last few days of the season — pool records,
season history, submissions, and everything else are unaffected either way.

Once DNS has propagated and you've confirmed the offseason site is live, delete the Railway project from the Railway dashboard. This removes all three services (web, Postgres, cron) and stops usage charges — but **not** the Hobby plan's monthly subscription, which is billed to the account, not the project. Step 13 takes care of that.

**Deletion is scheduled, not immediate** — confirmed 2026-09-08, both on this project and
on a throwaway one deleted the same day via CLI. Railway's own `railway delete --help`
says as much ("Project deletion is scheduled by Railway. Treat a successful response as
the deletion request being accepted."); in the dashboard it showed a ~48 hour delay before
the project actually disappears. It'll still list as existing in the meantime — that's
expected, not a failed deletion.

### 12. Resubmit the sitemap in Search Console

The sitemap URL is unchanged (`https://phillypools.app/sitemap.xml`) and now lists the
homepage plus every archived pool page, so there's nothing to reconfigure — but ask
Search Console to re-fetch it so the offseason set is picked up promptly. Over the next
few weeks, check Coverage: the pool URLs should stay indexed. If they start showing up
as "Page with redirect" or "Soft 404," something is shadowing the static pages — check
`_redirects` first.

### 13. Cancel the Railway Hobby plan subscription

Last step, once everything above is done and step 11's scheduled deletion has actually
gone through. In Railway's account billing settings, cancel the **Hobby plan**
subscription.

Deleting the project is not enough. The Hobby plan is a monthly subscription on the
account itself, and Railway keeps charging it with zero projects and nothing running.
Learned the hard way in the 2026 shutdown: the project was deleted 2026-09-08, and a
full extra month was still billed on 2026-09-11. Part 2 step 1 re-subscribes.

---

## Between the two — editing pool data mid-offseason

There's no live database in the offseason: **prod is the backup dump** from Part 1
step 6, so editing it is a prod write — Josh's call, same as in season. A custom-format
dump can't be edited in place. What worked on 2026-09-16 (moving did-not-open pools'
closure text from `updates` into `notes`):

1. Keep an untouched copy first (`cp -p X.dump X.dump.bak`) and leave it alone.
2. Restore into a throwaway local Postgres matching the dump's major version:
   `docker run -d --name pp-scratch-pg -e POSTGRES_PASSWORD=scratch -e POSTGRES_DB=railway -p 127.0.0.1:55432:5432 postgres:18`,
   then `pg_restore -d <url> --no-owner --no-acl X.dump`.
3. Make the edits in a transaction whose `WHERE` requires the exact current values,
   and set `last_updated = now()` on edited pools as an admin save would — the
   sitemap's `lastmod` reads it, which is what tells Google to recrawl.
4. `pg_dump -Fc` back over `X.dump`. Verify by restoring it into a second database and
   diffing plain-SQL dumps (ignore the random `\restrict` / `\unrestrict` lines).
5. Sync dev from the container: the `sync-prod-db.md` recipe with
   `DATABASE_URL=<container url>` on the `dumpdata` instead of `railway ssh`.
6. Re-render (step 3), **but keep the previously deployed `bot/index.html`** — see
   step 4's note on why a re-render undercounts it. Diff the new build against the old
   one before `wrangler pages deploy offseason-build`; only the pages you meant to
   change, their sitemap `lastmod`s, and nothing else should differ.
7. `docker rm -f pp-scratch-pg`.

## Part 2 — Start of Season Rebuild

Do these roughly in order. The database restore comes *before* the first web deploy,
and the DNS cutover is last — the app can run on a Railway URL for testing before you
point the real domain at it.

> **Read this before step 4.** Steps 4 and 5 below assume Railway reads `railway.toml`
> for the build and start commands, including the `RAILWAY_SERVICE_NAME` branch that
> makes one repo serve as both web and cron. That stopped being true on **2026-12-01**,
> when Railway's Config as Code deprecation took effect — the replacement is
> `.railway/railway.ts` (https://docs.railway.com/infrastructure-as-code).
>
> **Part 1 step 10.5 should have converted this already**, during the shutdown. Confirm
> `.railway/railway.ts` is in the repo before starting step 4. Step 10.5's throwaway-project
> test (2026-09-08) found IaC creates a service's *settings* from nothing, but doesn't
> connect it to a GitHub source — so steps 4 and 5 below **stay manual as written**
> (dashboard, "add from GitHub repo"), with one addition: run `npm install railway` at
> the repo root, then `railway config plan` and `railway config apply` against each
> service right after connecting it, rather than assuming the dashboard's defaults match
> `.railway/railway.ts`. If that conversion never happened, do it now, before creating
> anything. Once this has actually been proven out on a live rebuild, simplify steps 4
> and 5 to state that directly and delete both this note and step 10.5.

### 1. Create a new Railway project

Re-subscribe to the Railway **Hobby plan** first, since Part 1 step 13 canceled it. Then,
in the Railway dashboard, create a new project named `phillypools` (or similar).

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
pg_restore -d "<NEW_DATABASE_URL>" --no-owner offseason-backups/phillypools-YYYYMMDD.dump
```

This restores every table from the dump — pool records, season history, previous
submissions, site announcements, etc.

**Restore the `.dump`, not a `.dump.bak` next to it.** The 2026 backup
(`phillypools-20260907.dump`) was edited after shutdown (2026-09-16, pool notes — see
"editing pool data mid-offseason" above); `phillypools-20260907.dump.bak` is the
untouched original, kept only as a fallback.

Last season's `/stats/` history comes back with it, so year-over-year comparison
works immediately. Nothing needs configuring: `UsageEvent` starts empty (you cleared
it in Part 1), the nightly visitor salt regenerates itself on the first request, and
the rollup runs off the existing cron service rather than a schedule of its own.
Note that per-pool history is keyed by slug, so a pool renamed between seasons shows
its old slug for last season's rows and its new name for this season's.

### 3.5. 2027 relaunch only: credentials changed after Railway's October 2026 breach

This applies to the 2027 rebuild alone. It is a one-time cleanup, not a standing
step: no part of this runbook says to revoke or rotate keys at shutdown, and later
offseasons restore from the backup as usual unless something like this happens again.

Railway disclosed an environment-variable breach on 2026-10-21. The 2026 project had
been deleted about six weeks earlier, and Railway didn't classify the workspace as
higher-risk, but the keys that had lived in Railway were still valid, so on 2026-10-25
each one was either revoked or rolled:

- **`ANTHROPIC_API_KEY`: revoked, so create a new one.** Both keys that had been in
  Railway (`philly-pools` and `philly-pools-v2`) were deleted, and the 2026 backup files
  carry a `REVOKED-2026-10-25-…` marker instead of a value. Create a new prod key at
  platform.claude.com (e.g. `philly-pools-v4-prod`). Don't reuse `philly-pools-v3-dev`.
  That's the local `.env` key, and the reason for having it is that it has never been
  in Railway.
- **`R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`: rolled, so nothing to do.** The R2 API
  token was rolled in place rather than deleted, and the new values were written into
  `env-vars-web-2026.txt`, so restore them from the backup like the rest. Before the
  roll, the bucket showed 0 Class A (write) operations over the 30 days the dashboard
  checks easily, and a normal object count. If the token ever has to be recreated
  from scratch, it needs **Object Read & Write** on the `phillypools-submissions`
  bucket.
- **`SES_ACCESS_KEY_ID` / `SES_SECRET_ACCESS_KEY`: revoked, so create a new one.** Both
  access keys on the IAM user `phillypools-ses-sender` were revoked: `…3CEK`
  ("railway-ses-sender", last used about 2026-08-24) and `…TCOS`
  ("railway-ses-sender-v2", created at the 2026 shutdown and never used). The user
  itself was kept, so create a new access key on it in IAM and set the pair on the cron
  service in step 5. `SES_REGION` and the two `DIGEST_*` addresses are unchanged, so
  take them from the backup. IAM allows only two access keys per user, deactivated ones
  included, so if both old keys are still listed, delete them before creating the new
  one.

Step 5.5 verifies all of these as usual. At the 2027 shutdown, the new values go into
that year's backup files the normal way.

### 4. Deploy the web service

Add a new service from the GitHub repo (`phillypools`). `railway.toml` stopped being
read on 2026-12-01 (see the note above this Part) — once the service is connected, run
`npm install railway` at the repo root if you haven't already, then
`railway config plan` / `railway config apply` (linked to this service) to apply the
build/start/healthcheck settings from `.railway/railway.ts` instead.

Set the following environment variables on the web service:

```
SECRET_KEY=<from your saved backup>
ANTHROPIC_API_KEY=<from your saved backup; 2027 only: new key, see step 3.5>
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

Add a second service from the same GitHub repo, then run `railway config apply` (linked
to this service) the same as step 4 — `.railway/railway.ts` defines one service whose
start command branches on `RAILWAY_SERVICE_NAME`, so the same file covers both.
All of its variables go on the cron service itself (Variables tab of that service). Three are cross-service references to the web service (replace `phillypools` with whatever the web service is actually named in the Railway dashboard); the five SES digest-email vars are **plain literal values** from your saved backup — they exist only on the cron service and are not set on (or referenced from) the web service, since only the cron sends email:

```
RAILWAY_SERVICE_NAME=url-watcher
ANTHROPIC_API_KEY=${{phillypools.ANTHROPIC_API_KEY}}
DATABASE_URL=${{phillypools.DATABASE_URL}}
SECRET_KEY=${{phillypools.SECRET_KEY}}
SES_ACCESS_KEY_ID=<literal value from your saved backup; 2027 only: new key, see step 3.5>
SES_SECRET_ACCESS_KEY=<literal value from your saved backup; 2027 only: new key, see step 3.5>
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

### 5.5. Verify every restored credential still works

Anything pasted in from `offseason-backups/env-vars-*.txt` could have been rotated,
revoked, or quietly expired in the months since it was captured — a provider-side
rotation, an account cleanup, a value that got updated live and never written back to
the file. A credential that's *present* isn't the same as one that *works*, and most
of these fail silently (a dead map key just shows "API KEY REQUIRED" instead of an
error) rather than loudly, so nothing forces you to notice on its own. Check each one
now, before real traffic depends on it:

- **`ANTHROPIC_API_KEY`** — a real call, not just a pipeline that might short-circuit
  before reaching it:
  ```bash
  railway ssh --service web -- python manage.py shell -c \
    "import anthropic, os; c = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY']); print(c.messages.create(model='claude-haiku-4-5-20251001', max_tokens=8, messages=[{'role':'user','content':'hi'}]).content)"
  ```
  An auth error here means the key's dead; a printed response means it's good.
- **`CARTO_API_KEY`** — load the homepage map. Tiles stamped "API KEY REQUIRED"
  instead of the real basemap mean the key's dead; see `README.md`'s basemap section.
- **`CLOUDFLARE_TURNSTILE_SITE_KEY` / `_SECRET_KEY`** — can't be tested until step 7's
  DNS cutover (Turnstile is bound to the real hostname; see the Testing gotcha under
  step 4). Don't skip circling back — submit something real through `/submit/` on the
  live domain once it's up, and confirm the captcha actually passes rather than just
  assuming it will because nothing errored at deploy time.
- **`R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `R2_BUCKET_NAME`**
  — view a pool page with an existing display image (read path), then submit a test
  update with an uploaded photo (write path). Either credential being wrong drops
  silently to a broken image rather than a visible error.
- **`SES_ACCESS_KEY_ID` / `SES_SECRET_ACCESS_KEY` / `SES_REGION` / `DIGEST_FROM_EMAIL`
  / `DIGEST_TO_EMAIL`** — don't rely on `send_pending_digest` for this: it only sends
  when something's actually pending, and a fresh restore usually has an empty queue,
  so a clean run tells you nothing about whether SES itself works. Force a real send
  instead:
  ```bash
  railway ssh --service url-watcher -- python manage.py shell -c \
    "from pools.services.digest import _send_via_ses; _send_via_ses('[phillypools.app] SES credential test', 'If this arrived, SES credentials are good.')"
  ```
  Confirm the email actually lands in `DIGEST_TO_EMAIL`'s inbox — not just that the
  command exited without raising.

Anything that fails: get a fresh value from the provider, update it on the Railway
service (and in the saved env-var file, so this season's backup is the good copy),
and redeploy before moving on. A credential that's still broken once the cron is
unpaused fails silently there too — the whole point of checking now is not finding
out from a missed digest email in October.

### 6. Run the new season setup

Follow `season-setup.md` from the beginning.

### 7. Switch DNS back to Railway

Add both `phillypools.app` and `www.phillypools.app` as custom domains on the Railway web service (Railway dashboard → web service → Settings → Custom Domains). Update both DNS records — in **Cloudflare's** DNS dashboard, not Namecheap; DNS moved there during the 2026 shutdown (Part 1 step 10) and Namecheap is registrar-only now — to point to the Railway-provided CNAMEs. Verify the live app loads correctly on both.

The zone's `www` → apex Bulk Redirect (Part 1 step 10) can stay on all season. With
the `www` record proxied it answers first with the same 301 `WWWRedirectMiddleware`
would send; with it DNS-only it simply stops applying and the middleware takes over.

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
