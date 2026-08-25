"""The User-Agent strings this app sends, defined in exactly one place.

Three tokens, because the honest answer to "what are you?" is different depending
on which call you caught:

- `CRAWLER` is a robot on a schedule, fetching a handful of known URLs whether or
  not anyone asked. That's the one a sysadmin might reasonably want to throttle.
- `SUBMISSION` is not a crawler. It's a single fetch of a single URL that a person
  pasted into the submit form seconds earlier, and it follows no links. Calling it
  a bot would be inaccurate, and would also — incidentally — trip the naive
  `/bot|crawler|spider/i` blocklists that some small sites run, on the one call
  where a block leaves a human waiting.
- `ADMIN` is a one-off backfill someone ran by hand from a terminal.

All three keep the `Mozilla/5.0 (compatible; ...)` prefix. It reads like a
disguise, but it's what Googlebot, bingbot and Applebot all send: some WAFs reject
anything that doesn't start with it, and www.phila.gov is one of them — it answers
a bare `python-requests` UA with 403. The honesty lives in the product token and
the `+URL`, which is a page explaining who we are and how to make us stop.

The /bot page renders its table from these constants, so it can't describe a string
we don't actually send.
"""

VERSION = "1.0"

# Must stay in sync with the `bot` route in pools/urls.py *and* the file
# render_static_site writes for the offseason. A 404 here is worse than no URL at
# all, and the offseason is exactly when an annoyed sysadmin would come looking.
INFO_URL = "https://phillypools.app/bot/"


def _ua(token):
    return f"Mozilla/5.0 (compatible; {token}/{VERSION}; +{INFO_URL})"


CRAWLER = _ua("PhillyPoolsBot")
SUBMISSION = _ua("PhillyPools-Submission")
ADMIN = _ua("PhillyPools-Admin")

# Nominatim's usage policy requires a genuine contact address in the UA, and it
# wants an application name rather than a browser string, so this one deliberately
# doesn't follow the pattern above.
GEOCODER = f"PhillyPools/{VERSION} (+{INFO_URL}; josh@josh-rosenberg.com)"

CRAWLER_HEADERS = {"User-Agent": CRAWLER}
SUBMISSION_HEADERS = {"User-Agent": SUBMISSION}
ADMIN_HEADERS = {"User-Agent": ADMIN}

# What the /bot page lists. Ordered most-likely-to-be-seen first; the description
# is what someone reading their access log needs to know about that string.
PUBLIC_AGENTS = [
    {
        "name": f"PhillyPoolsBot/{VERSION}",
        "string": CRAWLER,
        "when": "Scheduled checks, a few times a day, unattended.",
        "what": (
            "A handful of known pages on phila.gov, plus the city's public ArcGIS "
            "pool layer. It follows no links and crawls nothing else."
        ),
    },
    {
        "name": f"PhillyPools-Submission/{VERSION}",
        "string": SUBMISSION,
        "when": "Only when a visitor pastes a link into our submit form.",
        "what": (
            "That one URL, once, so we can read what it says about a pool's hours. "
            "No crawling, no link-following, and nothing behind a login."
        ),
    },
    {
        "name": f"PhillyPools-Admin/{VERSION}",
        "string": ADMIN,
        "when": "Rarely — a one-off backfill run by hand.",
        "what": "Whatever page that particular job needs. Not on a schedule.",
    },
]
