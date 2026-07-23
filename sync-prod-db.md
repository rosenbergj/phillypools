# Sync prod DB into dev

1. Install the Railway CLI:

   ```bash
   curl -fsSL https://railway.app/install.sh | sh
   ```

   (No npm/brew on this machine, so the curl script is the way. It may need a PATH update afterward.)

2. One-time: register an SSH key with Railway (needed for step 3 below):

   ```bash
   railway ssh keys add
   ```

   Auto-detects your local public key and registers it with your Railway account. The first `railway ssh` connection will also prompt you (interactively, in your own terminal — not scriptable) to accept Railway's SSH host key; do that once before continuing.

3. From the project root, with your venv active:

   ```bash
   source .venv/bin/activate

   # Dump prod data by SSHing into the web service and running dumpdata there, streaming
   # the output back to a local file. This runs inside Railway's private network, where
   # postgres.railway.internal (the value of DATABASE_URL) actually resolves — it doesn't
   # from your laptop, and this project has no public DATABASE_PUBLIC_URL variable to fall
   # back to. (No .venv/bin/ prefix here: `python` is whatever Railway's own build set up
   # inside the container, unrelated to your local venv/pyenv setup.)
   #    - excludes PoolLike (visitor IP addresses)
   #    - excludes UsageEvent/VisitorSalt for the same reason: the raw usage rows
   #      carry per-visitor pseudonyms and there's no reason to copy them onto a
   #      laptop. UsageDaily (aggregate counts only) does come along, so /stats/
   #      has something to show locally.
   #    - excludes contenttypes/permissions/sessions/admin logs — Django regenerates these
   #      locally and loading prod's copies causes PK collisions against a fresh local DB
   railway ssh --service web -- python manage.py dumpdata \
     --exclude pools.PoolLike \
     --exclude pools.UsageEvent \
     --exclude pools.VisitorSalt \
     --exclude contenttypes \
     --exclude auth.permission \
     --exclude sessions.session \
     --exclude admin.logentry \
     > prod_dump.json

   # Reset local sqlite DB to a clean schema
   rm db.sqlite3
   .venv/bin/python manage.py migrate

   # Migration 0020 seeds a default heat-emergency MonitoredPage row on every fresh
   # migrate (needed for normal deploys). It collides with loaddata restoring the same
   # row from prod (same unique url, different pk) — clear it first since the dump
   # already contains the authoritative copy and nothing else FKs to MonitoredPage.
   .venv/bin/python manage.py shell -c "from pools.models import MonitoredPage; MonitoredPage.objects.all().delete()"

   # Load prod data in
   .venv/bin/python manage.py loaddata prod_dump.json

   # Delete the dump — it includes your prod admin user's password hash, don't leave it on disk
   rm prod_dump.json
   ```

   Uses `.venv/bin/python` explicitly for the local steps rather than bare `python` — if pyenv-virtualenv is installed, its `PROMPT_COMMAND` hook can silently deactivate a plain (non-pyenv) venv like this one on the next prompt, so `python` on PATH can revert to a pyenv shim without Django installed even though you just activated the venv.

Two things to know going in:

1. **`auth.user` is included by default**, so your prod admin login (hashed password and all) will work locally too. If you'd rather not have that hash sitting in a file even briefly, add `--exclude auth.user` to the dumpdata command and run `.venv/bin/python manage.py createsuperuser` locally instead.
2. **Images won't come along.** Submission photos and pool display images live in Cloudflare R2 in prod, but local dev has no R2 credentials in `.env`, so it falls back to local filesystem storage. The dump only carries the image *path* referenced in the DB, not the file itself — so any pool with a display image, or any submission with an uploaded photo, will show a broken image locally after the sync. Fine for testing schedule/text-based features; not useful for testing image display without also fetching the files from R2.
