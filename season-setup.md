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

This clears opening/closing dates, their source URLs, updates, and weekday/weekend schedule text for all pools. Before clearing schedules, it archives them into each pool's season history record, so the site can show "here was last year's schedule" on pool detail pages until a new schedule is submitted for the current season. (Only the immediately prior year is shown this way; older history is retained in the database but not displayed.)

Any open-ended schedule change (no end date set — e.g. an emergency closure that was never resolved) is closed out using the pool's just-cleared closing date, or Dec 31 of the year it started if the pool had no closing date. It (and all other past schedule changes) is then deleted.

It will also prompt you to clear the selected display image for each pool. Display images are typically specific to a season (a photo of last year's hours sign won't reflect the new season's schedule), so you'll generally want to confirm this. The dry run shows which pools have a display image selected.

Add `--keep-schedules` to skip clearing and archiving schedule text — useful if you want to carry schedules forward as a starting point for the new season and update them manually.

## 2. Sync pool inventory from OpenDataPhilly

```
python manage.py scrape_pools
```

This is idempotent — safe to run multiple times. It will:

- **Create** any new pools the city added
- **Update** name, address, coordinates, type, and active status for existing pools
- **Append** any new city comments to pool notes (with a datestamp), without overwriting notes you've added manually
- **Warn** about pools in the DB that no longer appear in the city's feed — review those manually and decide whether to delete or keep them

> **Note:** `scrape_pools` uses OpenDataPhilly addresses, which are often park centroids rather than real entrance addresses. Run step 2b to fix them.

## 2b. Update addresses from PPR schedule pages

```
python manage.py import_ppr_addresses --apply
```

This fetches the phila.gov pool opening schedule pages and updates addresses to match what PPR actually publishes (which aligns with Google Maps and other sources). It covers ~57 of the 70 pools — the rest either don't appear on schedule pages (inactive pools) or have name mismatches that require manual attention. Check the command output for any unmatched pools.

If you need to re-run `scrape_pools` later (e.g. to pick up a newly added pool), use `--fields` to avoid clobbering the PPR addresses:

```
python manage.py scrape_pools --apply --fields latitude longitude neighborhood pool_type
```

## 3. Review flagged pools

If the scraper printed a "not found in current feed" warning, check each listed pool in the admin. Options:

- Pool was removed by the city → delete it or set `is_active = False` with a note explaining why
- Pool was renamed or re-IDed → manually update `ppr_amenity_id` to match the new feed entry, then re-run the scraper

## 4. Check for pools whose active status changed

The scraper updates `is_active` automatically from the city's data, but it's worth a quick scan in the admin (`/admin/pools/pool/`) for anything that looks wrong — a pool that was active last year but is now marked inactive (or vice versa) is worth a second look before the season starts.

## 5. Review open-ended site announcements

`reset_season` doesn't touch `SiteAnnouncement` — that's a separate, intentionally season-agnostic model. Check the admin (`/admin/pools/siteannouncement/`) for any announcement with no end date; it's still showing on the site and will keep showing into the new season unless you update or delete it.
