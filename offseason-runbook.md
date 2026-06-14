# Offseason Runbook

Two halves: **shutting down at end of season** and **rebuilding at start of next season.**

---

## Part 1 — End of Season Shutdown

Do these steps in order. The goal is to back everything up before deleting the Railway project.

### 1. Clear the submission queue

Review and resolve all pending submissions in the admin (`/admin/pools/submission/`) before shutting down. Approved submissions should already be applied to pool records. Reject or delete anything leftover so you start the next season clean.

### 2. Delete monitored pages

Visit `/admin/pools/monitoredpage/` and delete all monitored pages. The URLs being watched are likely to change year to year (PPR updates their site, pool social links change, etc.), so it's cleaner to start fresh next season than to restore stale ones from the backup. Do this before the database dump so they don't end up in the backup.

### 3. Back up the database

From your local machine with the Railway CLI installed:

```bash
railway link          # select the phillypools project
railway connect Postgres
```

Or get the connection string from the Railway dashboard (Postgres service → Variables → `DATABASE_URL`) and run:

```bash
pg_dump "<DATABASE_URL>" -Fc -f phillypools-$(date +%Y%m%d).dump
```

Keep this `.dump` file somewhere safe — it contains all pool data, season history, and submission history. A password manager attachment or personal cloud storage works fine.

### 4. Record all environment variables

From the Railway dashboard, copy every env var from both the **web service** and the **cron service** to your password manager or a secure note. The ones that matter and won't be auto-regenerated:

- `SECRET_KEY`
- `ANTHROPIC_API_KEY`
- `CLOUDFLARE_TURNSTILE_SITE_KEY`
- `CLOUDFLARE_TURNSTILE_SECRET_KEY`
- `R2_ACCOUNT_ID`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_BUCKET_NAME`

`DATABASE_URL` and `RAILWAY_PUBLIC_DOMAIN` will be different on the new project — don't bother saving them.

> **Note:** R2 media uploads (pool photos, submission images) live in Cloudflare R2 independently of Railway and don't need to be backed up — they'll still be there next season.

### 5. Deploy the offseason page to Cloudflare Pages

If not already set up as a Cloudflare Pages project, create one pointing at the `offseason/` directory (via `wrangler pages deploy offseason/` or the Cloudflare dashboard → Pages → upload `offseason/`).

If it was set up last offseason, no redeployment is needed unless you changed `offseason/index.html`. You can preview it via the Cloudflare Pages URL before switching DNS in the next step.

### 6. Switch DNS to Cloudflare Pages

In your DNS provider, update the `phillypools.app` record to point to the Cloudflare Pages URL instead of the Railway-provided domain. Verify the offseason page loads correctly.

### 7. Delete the Railway project

Once DNS has propagated and you've confirmed the offseason page is live, delete the Railway project from the Railway dashboard. This removes all three services (web, Postgres, cron) and stops billing.

---

## Part 2 — Start of Season Rebuild

Do these roughly in order, but the DNS cutover is the last step — the app can be running on a Railway URL for testing before you point the real domain at it.

### 1. Create a new Railway project

In the Railway dashboard, create a new project named `phillypools` (or similar).

### 2. Add a PostgreSQL service

Add the Railway Postgres plugin/service. Once provisioned, Railway will inject `DATABASE_URL` into services in the same project automatically.

### 3. Deploy the web service

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
```

Use Railway's cross-service reference syntax (`${{ServiceName.VAR_NAME}}`) for `DATABASE_URL` rather than a hardcoded connection string — this way it stays correct if Railway rotates credentials. Check your saved env var notes to see which other variables were also set as cross-service references (rather than literal values) and replicate that.

`RAILWAY_PUBLIC_DOMAIN` is injected automatically — don't set it manually.

### 4. Restore the database

Get the new `DATABASE_URL` from the Railway Postgres service variables, then restore:

```bash
pg_restore -d "<NEW_DATABASE_URL>" --no-owner phillypools-YYYYMMDD.dump
```

This restores all pool records, season history, and previous submissions.

Verify the restore worked by visiting the Railway-provided URL for the web service and checking the admin.

### 5. Add the cron service

Add a second service from the same GitHub repo. It only needs three env vars, all set as cross-service references to the web service (replace `phillypools` with whatever the web service is actually named in the Railway dashboard):

```
RAILWAY_SERVICE_NAME=url-watcher
ANTHROPIC_API_KEY=${{phillypools.ANTHROPIC_API_KEY}}
DATABASE_URL=${{phillypools.DATABASE_URL}}
SECRET_KEY=${{phillypools.SECRET_KEY}}
```

In the Railway dashboard, configure this service as a **Cron** with schedule `15 13,16,19,22 * * *` (runs at 1:15, 4:15, 7:15, and 10:15 PM UTC). The `railway.toml` start command already branches on `RAILWAY_SERVICE_NAME`, so this service will run `check_pool_schedule` when triggered.

### 6. Run the new season setup

Follow `season-setup.md` from the beginning.

### 7. Switch DNS back to Railway

Add `phillypools.app` as a custom domain on the Railway web service (Railway dashboard → web service → Settings → Custom Domains). Update your DNS record to point to the Railway-provided CNAME. Verify the live app loads correctly.

### 8. Set up monitored pages

If there are any monitored pages being carried over from last year, visit `/admin/pools/monitoredpage/` to confirm they're listed. Either way, add any new ones for the current season. The cron will establish a baseline content hash on its first run — no submission is created on that first check.
