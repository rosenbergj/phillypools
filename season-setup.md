# New Season Setup

> **If the Railway project was taken down last offseason:** follow `offseason-runbook.md` Part 2 first. It will direct you back here at step 6.

**Not urgent.** Pools with prior-season dates automatically show "TBD" on January 1 — the site is never in a broken state. Do this whenever it's convenient before the first submissions for the new season start arriving.

---

## 1. Clear stale season data

Run from the Railway console:

```
python manage.py reset_season --dry-run
```

Review the output, then apply:

```
python manage.py reset_season
```

This clears opening/closing dates, their source URLs, and the updates field for all pools. It also deletes past schedule changes. Weekday/weekend schedule text is kept by default (hours are often consistent year to year); add `--clear-schedules` if you want to wipe those too.

## 2. Sync pool inventory from OpenDataPhilly

```
python manage.py scrape_pools
```

This is idempotent — safe to run multiple times. It will:

- **Create** any new pools the city added
- **Update** name, address, coordinates, type, and active status for existing pools
- **Append** any new city comments to pool notes (with a datestamp), without overwriting notes you've added manually
- **Warn** about pools in the DB that no longer appear in the city's feed — review those manually and decide whether to delete or keep them

## 3. Review flagged pools

If the scraper printed a "not found in current feed" warning, check each listed pool in the admin. Options:

- Pool was removed by the city → delete it or set `is_active = False` with a note explaining why
- Pool was renamed or re-IDed → manually update `ppr_amenity_id` to match the new feed entry, then re-run the scraper

## 4. Check for pools whose active status changed

The scraper updates `is_active` automatically from the city's data, but it's worth a quick scan in the admin (`/admin/pools/pool/`) for anything that looks wrong — a pool that was active last year but is now marked inactive (or vice versa) is worth a second look before the season starts.
