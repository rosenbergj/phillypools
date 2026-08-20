import tempfile
from datetime import date, datetime, time, timedelta, timezone as utc_tz
from pathlib import Path
from unittest.mock import patch
from xml.dom import minidom

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from pools.models import (
    MonitoredPage, Pool, ScheduleChange, Submission, SubmissionThrottle, UsageDaily,
    UsageEvent, UsageRollupState, VisitorSalt,
)
from pools.services import datacenter, usage
from pools.services.page_monitor import check_pool_info_page, start_monitoring
from pools.services.usage import USAGE_RAW_RETENTION_DAYS
from pools.views import SUBMISSION_DAILY_LIMIT, SUBMISSION_DAILY_LIMIT_STAFF, nearby_pools_context


def _reset_salt_cache():
    usage._salt_cache["day"] = None
    usage._salt_cache["salt"] = None


def _fake_request(ip="203.0.113.7", ua="Mozilla/5.0 (Macintosh)"):
    req = RequestFactory().get("/")
    req.META["REMOTE_ADDR"] = ip
    req.META["HTTP_USER_AGENT"] = ua
    return req


class VisitorHashTests(TestCase):
    """The privacy promises made on /stats/ are only true if these hold."""

    def setUp(self):
        _reset_salt_cache()

    def test_same_visitor_same_day_hashes_alike(self):
        day = timezone.localdate()
        self.assertEqual(
            usage.visitor_hash(_fake_request(), day),
            usage.visitor_hash(_fake_request(), day),
        )

    def test_different_visitors_differ(self):
        day = timezone.localdate()
        self.assertNotEqual(
            usage.visitor_hash(_fake_request(ip="203.0.113.7"), day),
            usage.visitor_hash(_fake_request(ip="198.51.100.2"), day),
        )

    def test_same_visitor_unlinkable_across_days(self):
        today = timezone.localdate()
        first = usage.visitor_hash(_fake_request(), today)
        # Simulate the next day: the old salt is gone and a fresh one is generated.
        _reset_salt_cache()
        VisitorSalt.objects.all().delete()
        second = usage.visitor_hash(_fake_request(), today - timedelta(days=1))
        self.assertNotEqual(first, second)

    def test_only_one_salt_is_ever_stored(self):
        today = timezone.localdate()
        usage.visitor_hash(_fake_request(), today)
        _reset_salt_cache()
        usage.visitor_hash(_fake_request(), today + timedelta(days=1))
        self.assertEqual(VisitorSalt.objects.count(), 1)


class ClassificationTests(TestCase):
    def test_crawlers_are_flagged(self):
        for ua in [
            "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
            "python-requests/2.31.0",
            "ClaudeBot/1.0",
            "",
        ]:
            self.assertEqual(usage.classify_client(ua), "bot", ua)

    def test_ordinary_browser_is_not_flagged(self):
        ua = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
        self.assertEqual(usage.classify_client(ua), "unknown")
        self.assertEqual(usage.classify_device(ua), "mobile")

    def test_referrer_keeps_host_only_and_drops_internal(self):
        self.assertEqual(
            usage.referrer_host("https://www.reddit.com/r/philadelphia/comments/abc/?q=secret"),
            "reddit.com",
        )
        self.assertEqual(usage.referrer_host("https://phillypools.app/pools/x/"), "")

    def test_zip_is_validated(self):
        self.assertEqual(usage.clean_zip(" 19143 "), "19143")
        self.assertEqual(usage.clean_zip("not-a-zip"), "")


# A real, current Chrome on Windows, used below as the thing the forgeries imitate.
_REAL_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


class ForgedUserAgentTests(TestCase):
    """Strings no shipped browser sends, whatever they claim to be."""

    def test_self_contradicting_agents_are_flagged(self):
        for ua in [
            # A scanner announcing the payload it is probing for.
            "http://phillypools.app/wp-admin/install.php?step=1",
            # Chromium always ends its WebKit claim at 537.36; this one is a digit short.
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.3",
            # Gecko and WebKit in the same breath.
            "Mozilla/5.0 (Windows NT 6.2; x86) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Firefox/91.0.0.0 Safari/537.36",
            # Safari with no version at all.
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Safari",
        ]:
            with self.subTest(ua=ua[:40]):
                self.assertEqual(usage.classify_client(ua), "bot")

    def test_the_genuine_article_still_passes(self):
        self.assertEqual(usage.classify_client(_REAL_CHROME), "unknown")

    def test_newly_listed_agents_that_name_themselves(self):
        for ua in [
            "Hello from Palo Alto Networks, find out more about our scans in https://example",
            "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/138.0.0.0 Mobile Safari/537.36 (compatible; Google-Read-Aloud; +http://x)",
            "NetworkingExtension/8624.2.5.10.8 Network/5812.122.1 iOS/26.5.2",
        ]:
            with self.subTest(ua=ua[:40]):
                self.assertEqual(usage.classify_client(ua), "bot")


class RetiredBrowserVersionTests(TestCase):
    """A version too old to still be in anyone's hands is a costume, not a browser."""

    _SAFARI_13 = ("Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0.3 Mobile/15E148 "
                  "Safari/604.1")

    def test_safari_13_is_a_bot_however_ordinary_the_rest_of_it_looks(self):
        self.assertEqual(usage.classify_client(self._SAFARI_13), "bot")

    def test_the_verdict_does_not_swallow_neighbouring_versions(self):
        for version, family in [("13", "safari/13"), ("12", "safari/12"), ("14", "safari/14")]:
            ua = self._SAFARI_13.replace("Version/13.0.3", f"Version/{version}.0.3")
            with self.subTest(version=version):
                self.assertEqual(usage.ua_family(ua), family)
                self.assertEqual(
                    usage.classify_client(ua), "bot" if family in usage.BOT_UA_FAMILIES else "unknown"
                )

    def test_the_family_is_still_recorded_as_what_it_claimed(self):
        """The column holds the claim; the verdict lives in client_class."""
        self.assertEqual(usage.ua_family(self._SAFARI_13), "safari/13")


class StaleVersionTests(TestCase):
    """The floor itself. What it costs a visitor is decided in the rollup, which
    only holds it against someone who also never ran the page — see RollupTests."""

    def test_versions_at_or_below_the_floor_are_stale(self):
        floor = usage.STALE_VERSION_FLOOR["chrome"]
        self.assertTrue(usage.is_stale_version(f"chrome/{floor}"))
        self.assertTrue(usage.is_stale_version(f"chrome/{floor - 1}"))
        self.assertFalse(usage.is_stale_version(f"chrome/{floor + 1}"))

    def test_families_without_a_floor_are_never_stale(self):
        for family in ["safari/13", "firefox/98", "opera/80", "other", ""]:
            with self.subTest(family=family):
                self.assertFalse(usage.is_stale_version(family))

    def test_a_stale_version_is_not_a_verdict_on_its_own(self):
        """Unlike BOT_UA_FAMILIES, the floor never decides a single request: the
        classifier sees one fetch and cannot know whether the page will be run."""
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")
        self.assertEqual(usage.ua_family(ua), "chrome/142")
        self.assertTrue(usage.is_stale_version("chrome/142"))
        self.assertEqual(usage.classify_client(ua), "unknown")


class UaFamilyTests(TestCase):
    def test_families_and_major_versions(self):
        cases = [
            (_REAL_CHROME, "chrome/130"),
            ("Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/13.0.3 Mobile/15E148 Safari/604.1", "safari/13"),
            ("Mozilla/5.0 (X11; Linux x86_64; rv:142.0) Gecko/20100101 Firefox/142.0",
             "firefox/142"),
            ("", ""),
            ("Uptime-Kuma/1.23.17", "other"),
        ]
        for ua, expected in cases:
            with self.subTest(ua=ua[:40]):
                self.assertEqual(usage.ua_family(ua), expected)

    def test_ios_browsers_are_not_all_called_safari(self):
        """Every browser on iOS is WebKit underneath and signs off with Safari's token."""
        ios = ("Mozilla/5.0 (iPhone; CPU iPhone OS 26_5_2 like Mac OS X) AppleWebKit/605.1.15 "
               "(KHTML, like Gecko) {} Mobile/15E148 Safari/604.1")
        for token, expected in [
            ("CriOS/150.0.7871.113", "chrome-ios/150"),
            ("FxiOS/145.0", "firefox-ios/145"),
            ("EdgiOS/140.0.0.0", "edge-ios/140"),
            ("Version/26.5", "safari/26"),
        ]:
            with self.subTest(token=token):
                self.assertEqual(usage.ua_family(ios.format(token)), expected)
                self.assertEqual(usage.classify_client(ios.format(token)), "unknown")

    def test_chromium_relatives_are_not_all_called_chrome(self):
        """Edge, Opera and Samsung all carry a Chrome token, so order of testing matters."""
        edge = _REAL_CHROME + " Edg/130.0.0.0"
        self.assertEqual(usage.ua_family(edge), "edge/130")
        self.assertEqual(usage.ua_family(_REAL_CHROME + " OPR/115.0.0.0"), "opera/115")

    def test_the_string_itself_is_never_returned(self):
        family = usage.ua_family(_REAL_CHROME)
        self.assertNotIn("Windows", family)
        self.assertLess(len(family), 20)


class ForgedHeaderTests(TestCase):
    """A copied user-agent is one line of work; the headers around it are not."""

    def _request(self, ua=_REAL_CHROME, secure=False, **headers):
        req = RequestFactory().get("/", secure=secure, HTTP_USER_AGENT=ua, **headers)
        return req

    def _real_browser_headers(self):
        return {
            "HTTP_ACCEPT": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "HTTP_ACCEPT_LANGUAGE": "en-US,en;q=0.9",
            "HTTP_SEC_CH_UA": '"Chromium";v="130", "Google Chrome";v="130"',
        }

    def test_a_complete_browser_passes(self):
        req = self._request(secure=True, **self._real_browser_headers())
        self.assertFalse(usage.forged_browser_headers(req))
        self.assertEqual(usage.classify_request(req, navigation=True), "unknown")

    def test_chrome_without_client_hints_is_forged(self):
        headers = self._real_browser_headers()
        del headers["HTTP_SEC_CH_UA"]
        req = self._request(secure=True, **headers)
        self.assertEqual(usage.classify_request(req, navigation=True), "bot")

    def test_client_hints_are_only_expected_over_https(self):
        """They are a secure-context feature, so plain HTTP — local dev — must not trip it."""
        headers = self._real_browser_headers()
        del headers["HTTP_SEC_CH_UA"]
        req = self._request(secure=False, **headers)
        self.assertEqual(usage.classify_request(req, navigation=True), "unknown")

    def test_missing_language_or_wildcard_accept_is_forged(self):
        for drop, replace in [("HTTP_ACCEPT_LANGUAGE", None), ("HTTP_ACCEPT", "*/*")]:
            with self.subTest(header=drop):
                headers = self._real_browser_headers()
                if replace is None:
                    del headers[drop]
                else:
                    headers[drop] = replace
                req = self._request(secure=True, **headers)
                self.assertEqual(usage.classify_request(req, navigation=True), "bot")

    def test_only_navigations_are_checked(self):
        """The site's own fetch() calls send a narrower header set and must not be caught."""
        req = self._request(secure=True)
        self.assertEqual(usage.classify_request(req, navigation=False), "unknown")

    def test_a_client_claiming_nothing_is_not_accused_of_lying(self):
        """No browser claim means no contradiction — it stays merely unknown."""
        req = self._request(ua="Mozilla/5.0 (Macintosh)", secure=True)
        self.assertFalse(usage.forged_browser_headers(req))

    def test_ua_version_disagreeing_with_client_hints_is_forged(self):
        """A rotated UA string with the real engine's client hints still showing through."""
        headers = self._real_browser_headers()
        headers["HTTP_SEC_CH_UA"] = '"Chromium";v="130", "Google Chrome";v="130"'
        req = self._request(ua=_REAL_CHROME.replace("Chrome/130", "Chrome/145"), secure=True, **headers)
        self.assertEqual(usage.classify_request(req, navigation=True), "bot")

    def test_ua_version_agreeing_with_client_hints_passes(self):
        req = self._request(secure=True, **self._real_browser_headers())
        self.assertEqual(usage.classify_request(req, navigation=True), "unknown")


class DatacenterRangeTests(TestCase):
    def test_hosting_providers_are_recognised(self):
        for ip, provider in [
            ("43.164.1.211", "Tencent Cloud"),
            ("49.51.243.156", "Tencent Cloud"),
            ("49.234.192.248", "Tencent Cloud"),
            ("172.236.148.120", "Linode"),
            ("3.14.15.92", "AWS"),
            ("20.1.2.3", "Azure"),
            ("159.65.1.1", "DigitalOcean"),
        ]:
            with self.subTest(provider=provider):
                self.assertTrue(datacenter.is_datacenter_ip(ip))

    def test_people_are_not(self):
        for ip, isp in [
            ("73.30.56.95", "Comcast"),
            ("96.245.3.232", "Verizon FiOS"),
            ("174.198.198.96", "Verizon Wireless"),
            ("8.8.8.8", "Google public DNS"),
            ("1.1.1.1", "Cloudflare resolver"),
        ]:
            with self.subTest(isp=isp):
                self.assertFalse(datacenter.is_datacenter_ip(ip))

    def test_unparseable_input_is_not_an_error(self):
        """Measurement must never be the reason a page fails to load."""
        for value in ["", "not-an-ip", "999.1.1.1", None]:
            with self.subTest(value=value):
                self.assertFalse(datacenter.is_datacenter_ip(value))

    def test_ipv6_answers_no_rather_than_guessing(self):
        self.assertFalse(datacenter.is_datacenter_ip("2606:4700::1"))

    def test_relay_and_cdn_networks_are_left_out(self):
        """They carry real people; catching them would erase privacy-conscious visitors."""
        for ip, network in [("104.28.1.1", "Cloudflare"), ("151.101.1.1", "Fastly")]:
            with self.subTest(network=network):
                self.assertFalse(datacenter.is_datacenter_ip(ip))


class ProbePathTests(TestCase):
    def test_scanner_paths_are_recognised(self):
        for path in ["/wp-admin/install.php", "/.env", "/vendor/phpunit/x", "/.git/config"]:
            with self.subTest(path=path):
                self.assertTrue(usage.is_probe_path(path))

    def test_a_plausible_wrong_guess_is_not_a_probe(self):
        """Someone typing a page we might have had is looking for something, not rattling doors."""
        for path in ["/contact", "/contact-us", "/pools/no-such-pool/", "/about"]:
            with self.subTest(path=path):
                self.assertFalse(usage.is_probe_path(path))


class MiddlewareTests(TestCase):
    def setUp(self):
        _reset_salt_cache()
        self.pool = Pool.objects.create(
            ppr_amenity_id="t1", name="Test Pool", address="1 Main St", slug="test-pool"
        )

    def test_pool_view_is_recorded_with_slug(self):
        self.client.get(f"/pools/{self.pool.slug}/")
        event = UsageEvent.objects.get(event="pool_view")
        self.assertEqual(event.key, "test-pool")

    def test_filter_request_records_the_filter_used(self):
        self.client.get("/pools-json/?status=zumba&zip=19143")
        event = UsageEvent.objects.get(event="filter")
        self.assertEqual(event.status_filter, "zumba")
        self.assertEqual(event.zip_searched, "19143")

    def test_no_ip_or_user_agent_is_stored(self):
        self.client.get("/", HTTP_USER_AGENT="Mozilla/5.0 (Macintosh)", REMOTE_ADDR="203.0.113.9")
        event = UsageEvent.objects.get()
        stored = " ".join(str(v) for v in event.__dict__.values())
        self.assertNotIn("203.0.113.9", stored)
        self.assertNotIn("Macintosh", stored)

    def test_untracked_paths_are_ignored(self):
        self.client.get("/robots.txt")
        self.assertEqual(UsageEvent.objects.count(), 0)

    def test_signed_in_staff_are_labelled(self):
        from django.contrib.auth.models import User
        User.objects.create_superuser("admin", "a@example.com", "pw")
        self.client.login(username="admin", password="pw")
        self.client.get("/")
        self.assertEqual(UsageEvent.objects.get().client_class, "staff")

    def test_ordinary_visitors_are_not_labelled_staff(self):
        self.client.get("/", HTTP_USER_AGENT="Mozilla/5.0 (Macintosh)")
        self.assertEqual(UsageEvent.objects.get().client_class, "unknown")

    def test_the_browser_family_is_recorded_but_not_the_string(self):
        self.client.get("/", HTTP_USER_AGENT=_REAL_CHROME)
        event = UsageEvent.objects.get()
        self.assertEqual(event.ua_family, "chrome/130")
        self.assertNotIn("Windows", " ".join(str(v) for v in event.__dict__.values()))

    def test_speculative_fetches_are_not_visits(self):
        """A prefetch is a guess about what someone might do, not something they did."""
        for header, value in [
            ("HTTP_SEC_PURPOSE", "prefetch;anonymous-client-ip"),
            ("HTTP_SEC_PURPOSE", "prerender"),
            ("HTTP_PURPOSE", "prefetch"),
            ("HTTP_X_MOZ", "prefetch"),
            ("HTTP_X_PURPOSE", "preview"),
        ]:
            with self.subTest(header=f"{header}: {value}"):
                UsageEvent.objects.all().delete()
                self.client.get("/", HTTP_USER_AGENT=_REAL_CHROME, **{header: value})
                self.assertEqual(UsageEvent.objects.count(), 0)

    def test_a_prefetch_the_visitor_then_opens_is_counted_once_they_open_it(self):
        """Dropping the guess must not cost us the real visit that follows it."""
        self.client.get("/", HTTP_USER_AGENT=_REAL_CHROME, HTTP_SEC_PURPOSE="prefetch")
        self.assertEqual(UsageEvent.objects.count(), 0)
        self.client.get("/", HTTP_USER_AGENT=_REAL_CHROME)
        self.assertEqual(UsageEvent.objects.filter(event="index").count(), 1)

    def test_an_ordinary_request_is_still_recorded(self):
        self.client.get("/", HTTP_USER_AGENT=_REAL_CHROME, HTTP_SEC_FETCH_MODE="navigate")
        self.assertEqual(UsageEvent.objects.filter(event="index").count(), 1)

    def test_the_hosting_verdict_is_stored_but_not_the_address(self):
        self.client.get("/", HTTP_USER_AGENT=_REAL_CHROME, REMOTE_ADDR="43.164.1.211")
        event = UsageEvent.objects.get()
        self.assertTrue(event.datacenter)
        self.assertNotIn("43.164.1.211", " ".join(str(v) for v in event.__dict__.values()))

    def test_a_home_address_is_not_flagged(self):
        self.client.get("/", HTTP_USER_AGENT=_REAL_CHROME, REMOTE_ADDR="73.30.56.95")
        self.assertFalse(UsageEvent.objects.get().datacenter)

    def test_a_scanner_probe_marks_the_visitor(self):
        resp = self.client.get("/wp-admin/install.php", HTTP_USER_AGENT=_REAL_CHROME)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(UsageEvent.objects.filter(event="probe").count(), 1)

    def test_a_burst_of_probes_records_one_row(self):
        """A scanner works through hundreds of paths; the table only needs to know it happened."""
        for path in ["/wp-admin/install.php", "/.env", "/vendor/phpunit/x", "/xmlrpc.php"]:
            self.client.get(path, HTTP_USER_AGENT=_REAL_CHROME)
        self.assertEqual(UsageEvent.objects.filter(event="probe").count(), 1)

    def test_an_ordinary_404_is_not_a_probe(self):
        self.client.get("/pools/no-such-pool/", HTTP_USER_AGENT=_REAL_CHROME)
        self.assertEqual(UsageEvent.objects.count(), 0)

    def test_the_probe_path_itself_is_not_stored(self):
        self.client.get("/wp-admin/install.php", HTTP_USER_AGENT=_REAL_CHROME)
        event = UsageEvent.objects.get(event="probe")
        self.assertNotIn("wp-admin", " ".join(str(v) for v in event.__dict__.values()))

    def test_a_referrerless_legacy_id_hit_marks_the_visitor(self):
        self.client.get(f"/pools/{self.pool.pk}/", HTTP_USER_AGENT=_REAL_CHROME)
        self.assertEqual(UsageEvent.objects.filter(event="legacy_id").count(), 1)

    def test_a_legacy_id_hit_with_a_referrer_is_left_alone(self):
        """A search engine still linking the old URL is a real visit, not a scraper's walk."""
        self.client.get(
            f"/pools/{self.pool.pk}/",
            HTTP_USER_AGENT=_REAL_CHROME,
            HTTP_REFERER="https://www.google.com/search?q=philly+pools",
        )
        self.assertEqual(UsageEvent.objects.filter(event="legacy_id").count(), 0)

    def test_a_burst_of_legacy_id_hits_records_one_row(self):
        other = Pool.objects.create(
            ppr_amenity_id="t2", name="Other Pool", address="2 Main St", slug="other-pool"
        )
        self.client.get(f"/pools/{self.pool.pk}/", HTTP_USER_AGENT=_REAL_CHROME)
        self.client.get(f"/pools/{other.pk}/", HTTP_USER_AGENT=_REAL_CHROME)
        self.assertEqual(UsageEvent.objects.filter(event="legacy_id").count(), 1)


class SubmissionThrottleTests(TestCase):
    def setUp(self):
        _reset_salt_cache()
        self.pool = Pool.objects.create(
            ppr_amenity_id="t1", name="Test Pool", address="1 Main St", slug="test-pool"
        )

    def _visitor_for(self, ip, ua):
        req = _fake_request(ip, ua)
        return usage.visitor_hash(req, timezone.localdate())

    @patch("pools.views.threading.Thread")
    def test_under_the_limit_goes_through(self, mock_thread):
        ip, ua = "203.0.113.50", "Mozilla/5.0 (Macintosh)"
        resp = self.client.post(
            "/submit/",
            {"url": "https://example.com/photo", "pool_id": str(self.pool.pk)},
            REMOTE_ADDR=ip, HTTP_USER_AGENT=ua,
        )
        self.assertRedirects(resp, f"/submit/thanks/?pool={self.pool.pk}")
        self.assertEqual(Submission.objects.count(), 1)
        visitor = self._visitor_for(ip, ua)
        self.assertEqual(SubmissionThrottle.objects.get(visitor=visitor).count, 1)

    def test_over_the_limit_is_redirected_without_creating_a_submission(self):
        ip, ua = "203.0.113.51", "Mozilla/5.0 (Macintosh)"
        visitor = self._visitor_for(ip, ua)
        SubmissionThrottle.objects.create(
            day=timezone.localdate(), visitor=visitor, count=SUBMISSION_DAILY_LIMIT
        )
        resp = self.client.post(
            "/submit/",
            {"url": "https://example.com/photo", "pool_id": str(self.pool.pk)},
            REMOTE_ADDR=ip, HTTP_USER_AGENT=ua,
        )
        self.assertRedirects(resp, f"/submit/thanks/?pool={self.pool.pk}")
        self.assertEqual(Submission.objects.count(), 0)
        throttle = SubmissionThrottle.objects.get(visitor=visitor)
        self.assertEqual(throttle.count, SUBMISSION_DAILY_LIMIT + 1)

    @patch("pools.views.threading.Thread")
    def test_staff_get_a_higher_limit(self, mock_thread):
        from django.contrib.auth.models import User
        User.objects.create_superuser("admin", "a@example.com", "pw")
        self.client.login(username="admin", password="pw")
        ip, ua = "203.0.113.52", "Mozilla/5.0 (Macintosh)"
        visitor = self._visitor_for(ip, ua)
        SubmissionThrottle.objects.create(
            day=timezone.localdate(), visitor=visitor, count=SUBMISSION_DAILY_LIMIT + 5
        )
        resp = self.client.post(
            "/submit/",
            {"url": "https://example.com/photo", "pool_id": str(self.pool.pk)},
            REMOTE_ADDR=ip, HTTP_USER_AGENT=ua,
        )
        self.assertRedirects(resp, f"/submit/thanks/?pool={self.pool.pk}")
        self.assertEqual(Submission.objects.count(), 1)

    @patch("pools.views.threading.Thread")
    def test_staff_can_still_be_throttled_past_their_own_limit(self, mock_thread):
        from django.contrib.auth.models import User
        User.objects.create_superuser("admin", "a@example.com", "pw")
        self.client.login(username="admin", password="pw")
        ip, ua = "203.0.113.53", "Mozilla/5.0 (Macintosh)"
        visitor = self._visitor_for(ip, ua)
        SubmissionThrottle.objects.create(
            day=timezone.localdate(), visitor=visitor, count=SUBMISSION_DAILY_LIMIT_STAFF
        )
        resp = self.client.post(
            "/submit/",
            {"url": "https://example.com/photo", "pool_id": str(self.pool.pk)},
            REMOTE_ADDR=ip, HTTP_USER_AGENT=ua,
        )
        self.assertRedirects(resp, f"/submit/thanks/?pool={self.pool.pk}")
        self.assertEqual(Submission.objects.count(), 0)


class PoolClickTests(TestCase):
    """The three pool-click beacons: the map pin, the list entry, and the
    closest-pools box on a detail page."""

    # (endpoint, event recorded) — every case below runs against all of them.
    ROUTES = [
        ("/pin-click/", "pin_click"),
        ("/card-click/", "card_click"),
        ("/nearby-click/", "nearby_click"),
    ]

    def setUp(self):
        _reset_salt_cache()
        self.pool = Pool.objects.create(
            ppr_amenity_id="t1", name="Test Pool", address="1 Main St", slug="test-pool"
        )

    def test_click_is_recorded(self):
        for url, event in self.ROUTES:
            with self.subTest(url=url):
                resp = self.client.post(url, {"slug": "test-pool"})
                self.assertEqual(resp.status_code, 204)
                self.assertEqual(
                    UsageEvent.objects.filter(event=event, key="test-pool").count(), 1
                )

    def test_the_routes_stay_separate(self):
        """Same pool reached three ways; which way it was is the whole point."""
        for url, _ in self.ROUTES:
            self.client.post(url, {"slug": "test-pool"})
        for _, event in self.ROUTES:
            self.assertEqual(UsageEvent.objects.filter(event=event).count(), 1)

    def test_unknown_slug_is_ignored_without_error(self):
        for url, _ in self.ROUTES:
            with self.subTest(url=url):
                resp = self.client.post(url, {"slug": "no-such-pool"})
                self.assertEqual(resp.status_code, 204)
                self.assertEqual(UsageEvent.objects.count(), 0)

    def test_get_is_rejected(self):
        for url, _ in self.ROUTES:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 405)

    def test_rate_limit_caps_a_single_visitor(self):
        from pools.views import POOL_CLICK_DAILY_MAX
        day = timezone.localdate()
        visitor = usage.visitor_hash(_fake_request(ip="127.0.0.1", ua=""), day)
        for url, event in self.ROUTES:
            with self.subTest(url=url):
                UsageEvent.objects.bulk_create([
                    UsageEvent(day=day, event=event, key="test-pool", visitor=visitor)
                    for _ in range(POOL_CLICK_DAILY_MAX)
                ])
                resp = self.client.post(url, {"slug": "test-pool"})
                self.assertEqual(resp.status_code, 429)

    def test_the_nearby_beacon_records_where_the_visitor_is_going(self):
        """Not the page they left: that one is already in their day as a pool_view."""
        Pool.objects.create(
            ppr_amenity_id="t2", name="Other Pool", address="2 Main St", slug="other-pool"
        )
        self.client.post(
            "/nearby-click/", {"slug": "other-pool"}, HTTP_REFERER="https://phillypools.app/pools/test-pool/"
        )
        self.assertEqual(UsageEvent.objects.get(event="nearby_click").key, "other-pool")

    def test_each_route_has_its_own_budget(self):
        """A visitor who has exhausted their pin clicks can still report a list click."""
        from pools.views import POOL_CLICK_DAILY_MAX
        day = timezone.localdate()
        visitor = usage.visitor_hash(_fake_request(ip="127.0.0.1", ua=""), day)
        UsageEvent.objects.bulk_create([
            UsageEvent(day=day, event="pin_click", key="test-pool", visitor=visitor)
            for _ in range(POOL_CLICK_DAILY_MAX)
        ])
        self.assertEqual(
            self.client.post("/card-click/", {"slug": "test-pool"}).status_code, 204
        )


class PageViewBeaconTests(TestCase):
    def setUp(self):
        _reset_salt_cache()

    def test_beacon_confirms_a_passive_visitor(self):
        """A reader who never filters or clicks is confirmable only through this."""
        resp = self.client.post("/page-loaded/", HTTP_USER_AGENT="Mozilla/5.0 (Macintosh)")
        self.assertEqual(resp.status_code, 204)
        event = UsageEvent.objects.get(event="pageview_js")
        self.assertEqual(event.client_class, "unknown")
        self.assertEqual(event.key, "")

    def test_beacon_promotes_the_visitor_to_confirmed(self):
        """The whole point: a bare page load now counts toward confirmed browsers."""
        self.client.post("/page-loaded/", HTTP_USER_AGENT="Mozilla/5.0 (Macintosh)")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(
            UsageDaily.objects.get(
                day=timezone.localdate(), metric="visitors", key="js_confirmed"
            ).visitors,
            1,
        )

    def test_get_is_rejected(self):
        self.assertEqual(self.client.get("/page-loaded/").status_code, 405)

    def test_rate_limit_caps_a_single_visitor(self):
        from pools.views import PAGEVIEW_JS_DAILY_MAX
        day = timezone.localdate()
        visitor = usage.visitor_hash(_fake_request(ip="127.0.0.1", ua=""), day)
        UsageEvent.objects.bulk_create([
            UsageEvent(day=day, event="pageview_js", visitor=visitor)
            for _ in range(PAGEVIEW_JS_DAILY_MAX)
        ])
        # The cap is silent — a beacon has no reason to signal back to its caller.
        resp = self.client.post("/page-loaded/", REMOTE_ADDR="127.0.0.1", HTTP_USER_AGENT="")
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(
            UsageEvent.objects.filter(event="pageview_js").count(), PAGEVIEW_JS_DAILY_MAX
        )


class RollupTests(TestCase):
    def setUp(self):
        _reset_salt_cache()
        self.today = timezone.localdate()

    # ua_family defaults to something set, standing in for a row recorded under the
    # current rules. A blank one means "written before the user-agent checks existed",
    # which the rollup reads as an uncheckable visitor and files as a bot — so a test
    # that wants an ordinary visitor has to look like a row from after the change.
    # The version has to be a current one for the same reason: one at or below
    # STALE_VERSION_FLOOR is filed as a bot unless it runs the page's JavaScript.
    def _event(self, day, event="index", visitor="aaa", client_class="unknown",
               ua_family="chrome/150", **kw):
        return UsageEvent.objects.create(
            day=day, event=event, visitor=visitor, client_class=client_class,
            ua_family=ua_family, **kw
        )

    def test_visitors_are_counted_distinctly_and_bots_excluded(self):
        self._event(self.today, visitor="aaa")
        self._event(self.today, visitor="aaa", event="pool_view", key="p")
        self._event(self.today, visitor="bbb")
        self._event(self.today, visitor="ccc", client_class="bot")

        call_command("rollup_usage", verbosity=0)

        self.assertEqual(
            UsageDaily.objects.get(day=self.today, metric="visitors", key="").visitors, 2
        )
        self.assertEqual(
            UsageDaily.objects.get(day=self.today, metric="visitors", key="bot").visitors, 1
        )

    def test_staff_are_counted_as_visitors_and_broken_out(self):
        """Staff stay inside the visitor total — the tile reports their share, not a deduction."""
        self._event(self.today, visitor="aaa")
        self._event(self.today, visitor="me", client_class="staff")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(
            UsageDaily.objects.get(day=self.today, metric="visitors", key="").visitors, 2
        )
        self.assertEqual(
            UsageDaily.objects.get(day=self.today, metric="visitors", key="staff").visitors, 1
        )

    def test_js_confirmed_requires_a_js_only_request(self):
        self._event(self.today, visitor="aaa")                      # page load only
        self._event(self.today, visitor="bbb", event="filter")      # ran the page's JS
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(
            UsageDaily.objects.get(day=self.today, metric="visitors", key="js_confirmed").visitors, 1
        )

    def _journey(self, key):
        return UsageDaily.objects.get(day=self.today, metric="journey", key=key).visitors

    def _at(self, hour):
        """A local wall-clock time on the day under test. auto_now_add can't be set
        on create, so callers write it over the row afterwards with .update()."""
        return timezone.make_aware(datetime.combine(self.today, time(hour, 30)))

    def _hour(self, key, audience):
        return UsageDaily.objects.get(
            day=self.today, metric="hour", key=key, audience=audience
        ).visitors

    def test_hours_are_recorded_on_philadelphia_s_clock(self):
        """Rows store UTC, but "when do people check" is a question about local time."""
        event = self._event(self.today, visitor="night-owl", event="pageview_js")
        at = self._at(22)
        UsageEvent.objects.filter(pk=event.pk).update(created_at=at)
        # The same instant is the small hours of the next day in UTC, so a rollup
        # that skipped the conversion would file this under a different hour.
        self.assertNotEqual(at.astimezone(utc_tz.utc).hour, 22)

        call_command("rollup_usage", verbosity=0)

        self.assertEqual(self._hour("22", "confirmed"), 1)
        self.assertFalse(
            UsageDaily.objects.filter(day=self.today, metric="hour")
            .exclude(key="22").exists()
        )

    def test_a_visitor_counts_in_every_hour_they_turn_up_in(self):
        """Hours say when people were here, so they deliberately don't sum to the day."""
        morning = self._event(self.today, visitor="twice", event="pageview_js")
        evening = self._event(self.today, visitor="twice", event="pool_view", key="p")
        quiet = self._event(self.today, visitor="quiet")  # never ran the page's JavaScript
        UsageEvent.objects.filter(pk=morning.pk).update(created_at=self._at(9))
        UsageEvent.objects.filter(pk=evening.pk).update(created_at=self._at(18))
        UsageEvent.objects.filter(pk=quiet.pk).update(created_at=self._at(9))

        call_command("rollup_usage", verbosity=0)

        self.assertEqual(self._hour("09", "confirmed"), 1)
        self.assertEqual(self._hour("18", "confirmed"), 1)
        # One person, counted in both hours, and still one confirmed visitor that day.
        self.assertEqual(
            UsageDaily.objects.get(
                day=self.today, metric="visitors", key="js_confirmed"
            ).visitors, 1
        )
        # The unconfirmed visitor shows up in the wider audience only.
        self.assertEqual(self._hour("09", "human"), 2)
        self.assertEqual(self._hour("18", "human"), 1)

    def test_journeys_split_confirmed_browsers_by_page(self):
        # Two pages, so multi-page whether or not they touched anything.
        self._event(self.today, visitor="multi")
        self._event(self.today, visitor="multi", event="pageview_js")
        self._event(self.today, visitor="multi", event="pool_view", key="p")
        # One page, but clicked a pin.
        self._event(self.today, visitor="engaged")
        self._event(self.today, visitor="engaged", event="pin_click", key="p")
        # One page (the list), beacon only.
        self._event(self.today, visitor="passive_list")
        self._event(self.today, visitor="passive_list", event="pageview_js")
        # One page (a pool's detail page), beacon only.
        self._event(self.today, visitor="passive_detail", event="pool_view", key="p")
        self._event(self.today, visitor="passive_detail", event="pageview_js")

        call_command("rollup_usage", verbosity=0)

        self.assertEqual(self._journey("multi_page"), 1)
        self.assertEqual(self._journey("single_engaged"), 1)
        self.assertEqual(self._journey("single_passive_list"), 1)
        self.assertEqual(self._journey("single_passive_detail"), 1)
        self.assertEqual(self._journey("single_passive_other"), 0)

    def test_journeys_add_back_up_to_the_confirmed_total(self):
        """The journey buckets partition confirmed browsers — no one counted twice or lost."""
        self._event(self.today, visitor="aaa", event="pageview_js")
        self._event(self.today, visitor="bbb", event="filter")
        self._event(self.today, visitor="ccc", event="pageview_js")
        self._event(self.today, visitor="ccc", event="pool_view", key="p")

        call_command("rollup_usage", verbosity=0)

        confirmed = UsageDaily.objects.get(
            day=self.today, metric="visitors", key="js_confirmed"
        ).visitors
        total = sum(
            self._journey(k) for k in (
                "multi_page", "single_engaged",
                "single_passive_list", "single_passive_detail", "single_passive_other",
            )
        )
        self.assertEqual(total, confirmed)

    def test_a_list_click_counts_as_using_the_page(self):
        """Browsing by list card used to be invisible and filed as 'read one page and left'."""
        self._event(self.today, visitor="lister", event="pageview_js")
        self._event(self.today, visitor="lister", event="card_click", key="p")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(self._journey("single_engaged"), 1)
        self.assertEqual(self._journey("single_passive_list"), 0)
        self.assertEqual(self._journey("single_passive_detail"), 0)

    def test_list_clicks_are_broken_down_per_pool(self):
        self._event(self.today, visitor="aaa", event="card_click", key="mander")
        self._event(self.today, visitor="bbb", event="card_click", key="mander")
        call_command("rollup_usage", verbosity=0)
        # A card click is itself proof of JavaScript, so both audiences agree here.
        self.assertEqual(
            UsageDaily.objects.get(
                day=self.today, metric="card_click", key="mander", audience="confirmed"
            ).visitors, 2
        )

    def test_nearby_clicks_are_broken_down_per_pool(self):
        """
        Which pools the closest-pools box actually sends people to, kept permanently:
        the raw rows carrying it are pruned, and the pool_view it produces says
        nothing about where it came from.
        """
        self._event(self.today, visitor="aaa", event="nearby_click", key="mander")
        self._event(self.today, visitor="bbb", event="nearby_click", key="mander")
        self._event(self.today, visitor="bbb", event="nearby_click", key="cobbs-creek")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(
            UsageDaily.objects.get(
                day=self.today, metric="nearby_click", key="mander", audience="confirmed"
            ).visitors, 2
        )
        self.assertEqual(
            UsageDaily.objects.get(
                day=self.today, metric="nearby_click", key="cobbs-creek", audience="confirmed"
            ).visitors, 1
        )

    def test_a_nearby_click_confirms_and_engages_the_visitor(self):
        """Only the page's own JavaScript can reach that endpoint, and choosing the
        next pool to look at is using the page, not just reading it."""
        self._event(self.today, visitor="hopper", event="pool_view", key="p")
        self._event(self.today, visitor="hopper", event="nearby_click", key="q")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(
            UsageDaily.objects.get(day=self.today, metric="visitors", key="js_confirmed").visitors, 1
        )
        self.assertEqual(self._journey("single_engaged"), 1)
        self.assertEqual(self._journey("single_passive_detail"), 0)

    def test_the_datacenter_rule_records_what_it_caught_by_browser(self):
        """Its verdict is stored as a bot; its working is what makes a poor
        confirmation rate diagnosable later."""
        self._event(self.today, visitor="rack1", ua_family="chrome/150", datacenter=True)
        self._event(self.today, visitor="rack2", ua_family="chrome/150", datacenter=True)
        self._event(self.today, visitor="rack3", ua_family="safari/17", datacenter=True)

        call_command("rollup_usage", verbosity=0)

        self.assertEqual(
            UsageDaily.objects.get(
                day=self.today, metric="datacenter", key="chrome/150", audience="bot"
            ).visitors, 2
        )
        self.assertEqual(
            UsageDaily.objects.get(
                day=self.today, metric="datacenter", key="safari/17", audience="bot"
            ).visitors, 1
        )
        # And they stay out of the visitor figures, as before.
        self.assertEqual(self._visitors(), 0)

    def test_a_stale_version_that_never_ran_the_page_is_a_bot(self):
        self._event(self.today, visitor="frozen", ua_family="chrome/142")
        self._event(self.today, visitor="current", ua_family="chrome/150")

        call_command("rollup_usage", verbosity=0)

        self.assertEqual(self._visitors(), 1)
        self.assertEqual(
            UsageDaily.objects.get(day=self.today, metric="visitors", key="bot").visitors, 1
        )

    def test_a_stale_version_that_ran_the_page_is_spared(self):
        """The straggler on a laptop that spent the season in a drawer. Running the
        page's JavaScript settles what the version number only suggests."""
        self._event(self.today, visitor="straggler", ua_family="chrome/142")
        self._event(self.today, visitor="straggler", event="pageview_js", ua_family="chrome/142")

        call_command("rollup_usage", verbosity=0)

        self.assertEqual(self._visitors(), 1)
        self.assertEqual(
            UsageDaily.objects.get(day=self.today, metric="visitors", key="js_confirmed").visitors, 1
        )
        self.assertFalse(
            UsageDaily.objects.filter(day=self.today, metric="stale_version").exists()
        )

    def test_the_stale_version_rule_records_what_it_caught_by_browser(self):
        self._event(self.today, visitor="f1", ua_family="chrome/142")
        self._event(self.today, visitor="f2", ua_family="chrome/142")
        self._event(self.today, visitor="f3", ua_family="chrome/131")

        call_command("rollup_usage", verbosity=0)

        self.assertEqual(
            UsageDaily.objects.get(
                day=self.today, metric="stale_version", key="chrome/142", audience="bot"
            ).visitors, 2
        )
        self.assertEqual(
            UsageDaily.objects.get(
                day=self.today, metric="stale_version", key="chrome/131", audience="bot"
            ).visitors, 1
        )
        self.assertEqual(self._visitors(), 0)

    def test_the_floor_only_applies_to_families_it_names(self):
        """Safari 26 is a current release; the floor is Chrome's number, not a
        version number in general."""
        self._event(self.today, visitor="mac", ua_family="safari/26")
        self._event(self.today, visitor="phone", ua_family="safari/18")

        call_command("rollup_usage", verbosity=0)

        self.assertEqual(self._visitors(), 2)

    def test_a_confirmed_visitor_from_a_rack_is_filed_as_a_person(self):
        """Somebody on a VPN, which is who the rule is written to spare. They belong
        in the visitor counts, not in a tally of traffic the rule rejected."""
        self._event(self.today, visitor="vpn", ua_family="firefox/131", datacenter=True)
        self._event(self.today, visitor="vpn", event="pageview_js",
                    ua_family="firefox/131", datacenter=True)

        call_command("rollup_usage", verbosity=0)

        self.assertEqual(
            UsageDaily.objects.get(
                day=self.today, metric="datacenter", key="firefox/131", audience="confirmed"
            ).visitors, 1
        )
        self.assertFalse(
            UsageDaily.objects.filter(day=self.today, metric="datacenter", audience="bot").exists()
        )
        self.assertEqual(self._visitors(), 1)

    def test_browsers_are_counted_a_second_time_without_their_versions(self):
        """Chrome ships a major every four weeks and Safari one a year, so the
        versioned rows compare one release against a year's worth of another."""
        self._event(self.today, visitor="a", ua_family="chrome/151", event="pageview_js")
        self._event(self.today, visitor="a", ua_family="chrome/152")   # updated mid-day
        self._event(self.today, visitor="b", ua_family="chrome/152", event="pageview_js")
        self._event(self.today, visitor="c", ua_family="safari/26", event="pageview_js")
        self._event(self.today, visitor="d", ua_family="chrome-ios/150", event="pageview_js")

        call_command("rollup_usage", verbosity=0)

        def family(key, audience="human"):
            return UsageDaily.objects.get(
                day=self.today, metric="browser_family", key=key, audience=audience
            )

        # Two people, not three: one of them was on both majors during the day.
        self.assertEqual(family("chrome").visitors, 2)
        self.assertEqual(family("chrome").events, 3)
        self.assertEqual(family("safari").visitors, 1)
        # Chrome's iOS app is Safari's engine underneath, so it keeps its own row.
        self.assertEqual(family("chrome-ios").visitors, 1)
        # And the versioned breakdown is untouched — a frozen version is the
        # clearest crawler tell there is, which is what BOT_UA_FAMILIES rests on.
        self.assertEqual(
            UsageDaily.objects.get(
                day=self.today, metric="browser", key="chrome/152", audience="human"
            ).visitors, 2
        )

    def test_the_version_free_breakdown_is_stored_per_audience(self):
        """Same as every other ranked breakdown: /stats/ can switch between them
        long after the raw rows are gone."""
        self._event(self.today, visitor="quiet", ua_family="chrome/152")
        self._event(self.today, visitor="ran", ua_family="chrome/152", event="pageview_js")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(
            UsageDaily.objects.get(
                day=self.today, metric="browser_family", key="chrome", audience="human"
            ).visitors, 2
        )
        self.assertEqual(
            UsageDaily.objects.get(
                day=self.today, metric="browser_family", key="chrome", audience="confirmed"
            ).visitors, 1
        )

    def test_a_retired_browser_version_is_a_bot_wherever_it_came_from(self):
        """The rule is the family alone: no rack, and JavaScript that ran, and it is
        still filed as a bot. Read off the stored family, so it reaches rows written
        before the rule existed — which is what makes the past recomputable."""
        self._event(self.today, visitor="old1", ua_family="safari/13")
        self._event(self.today, visitor="old1", ua_family="safari/13", event="pageview_js")
        self._event(self.today, visitor="real", ua_family="safari/17")

        call_command("rollup_usage", verbosity=0)

        self.assertEqual(self._visitors(), 1)
        self.assertEqual(
            UsageDaily.objects.get(day=self.today, metric="visitors", key="bot").visitors, 1
        )
        self.assertEqual(
            UsageDaily.objects.get(day=self.today, metric="visitors", key="js_confirmed").visitors, 0
        )
        self.assertFalse(
            UsageDaily.objects.filter(day=self.today, metric="browser", key="safari/13").exists()
        )

    def test_unconfirmed_visitors_are_left_out_of_the_split(self):
        """A no-JS client can't be told apart from a reader who did nothing."""
        self._event(self.today, visitor="quiet")            # HTML only, never confirmed
        self._event(self.today, visitor="bot1", client_class="bot", event="pageview_js")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(self._journey("single_passive_list"), 0)
        self.assertEqual(self._journey("single_passive_detail"), 0)

    def _visitors(self, key=""):
        return UsageDaily.objects.get(day=self.today, metric="visitors", key=key).visitors

    def test_a_probe_discredits_everything_else_that_visitor_did(self):
        """The scanner also fetched the front page; that request is not a visit either."""
        self._event(self.today, visitor="scanner", event="index")
        self._event(self.today, visitor="scanner", event="probe")
        self._event(self.today, visitor="real", event="index")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(self._visitors(), 1)
        self.assertEqual(self._visitors("bot"), 1)

    def test_a_referrerless_legacy_id_hit_discredits_the_visitor(self):
        """Walking the retired numeric ID space with no referrer is a scraper, not a stray link click."""
        self._event(self.today, visitor="walker", event="pool_view")
        self._event(self.today, visitor="walker", event="legacy_id")
        self._event(self.today, visitor="real", event="index")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(self._visitors(), 1)
        self.assertEqual(self._visitors("bot"), 1)

    def test_one_bot_row_taints_the_whole_visit(self):
        """The forgery check only runs on navigations, so a spoofer's beacon must not readmit them."""
        self._event(self.today, visitor="spoofer", event="index", client_class="bot")
        self._event(self.today, visitor="spoofer", event="pageview_js")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(self._visitors(), 0)
        self.assertEqual(
            UsageDaily.objects.get(
                day=self.today, metric="visitors", key="js_confirmed"
            ).visitors, 0
        )

    def test_traffic_predating_the_agent_checks_is_written_off(self):
        """No family recorded means the new rules can never be run against it."""
        self._event(self.today, visitor="legacy", ua_family="")
        self._event(self.today, visitor="current", ua_family="chrome/150")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(self._visitors(), 1)
        self.assertEqual(self._visitors("bot"), 1)

    def test_a_confirmed_browser_survives_the_write_off(self):
        """Running the page's JavaScript settles it, whenever the row was recorded."""
        self._event(self.today, visitor="old_but_real", ua_family="")
        self._event(self.today, visitor="old_but_real", event="pageview_js", ua_family="")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(self._visitors(), 1)
        self.assertEqual(self._visitors("bot"), 0)

    def test_staff_survive_the_write_off_too(self):
        self._event(self.today, visitor="me", client_class="staff", ua_family="")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(self._visitors(), 1)
        self.assertEqual(self._visitors("staff"), 1)

    def test_a_rack_that_never_ran_the_page_is_a_bot(self):
        self._event(self.today, visitor="scraper", datacenter=True)
        self._event(self.today, visitor="person")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(self._visitors(), 1)
        self.assertEqual(self._visitors("bot"), 1)

    def test_a_vpn_user_who_ran_the_page_is_not(self):
        """People browse from behind relays in those same ranges; running the JS settles it."""
        self._event(self.today, visitor="behind_vpn", datacenter=True)
        self._event(self.today, visitor="behind_vpn", event="pageview_js", datacenter=True)
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(self._visitors(), 1)
        self.assertEqual(self._visitors("bot"), 0)

    def test_staff_from_a_hosting_range_are_still_staff(self):
        self._event(self.today, visitor="me", client_class="staff", datacenter=True)
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(self._visitors("staff"), 1)
        self.assertEqual(self._visitors("bot"), 0)

    def test_browsers_are_broken_down(self):
        self._event(self.today, visitor="aaa", event="pageview_js", ua_family="chrome/150")
        self._event(self.today, visitor="bbb", event="pageview_js", ua_family="safari/18")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(
            UsageDaily.objects.get(
                day=self.today, metric="browser", key="chrome/150", audience="confirmed"
            ).visitors, 1
        )

    def test_reloading_one_page_is_not_two_pages(self):
        self._event(self.today, visitor="aaa", event="pageview_js")
        self._event(self.today, visitor="aaa")
        self._event(self.today, visitor="aaa")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(self._journey("single_passive_list"), 1)
        self.assertEqual(self._journey("multi_page"), 0)

    def test_two_different_pool_pages_count_as_two_pages(self):
        self._event(self.today, visitor="aaa", event="pool_view", key="one")
        self._event(self.today, visitor="aaa", event="pool_view", key="two")
        self._event(self.today, visitor="aaa", event="pageview_js")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(self._journey("multi_page"), 1)

    def test_ranked_breakdowns_are_stored_for_both_audiences(self):
        """Both populations must be written now — the raw rows to recompute them expire."""
        self._event(self.today, visitor="js", event="pageview_js")
        self._event(self.today, visitor="js", event="pool_view", key="seen")
        self._event(self.today, visitor="nojs", event="pool_view", key="seen")
        call_command("rollup_usage", verbosity=0)

        self.assertEqual(
            UsageDaily.objects.get(
                day=self.today, metric="pool_view", key="seen", audience="human"
            ).visitors, 2
        )
        self.assertEqual(
            UsageDaily.objects.get(
                day=self.today, metric="pool_view", key="seen", audience="confirmed"
            ).visitors, 1
        )

    def test_confirmed_event_count_leaves_out_the_page_load_beacon(self):
        """The beacon duplicates the page view it rides along with, so counting it doubles."""
        self._event(self.today, visitor="aaa")                        # page view
        self._event(self.today, visitor="aaa", event="pageview_js")   # its beacon
        self._event(self.today, visitor="aaa", event="filter")        # a real interaction
        call_command("rollup_usage", verbosity=0)

        row = UsageDaily.objects.get(day=self.today, metric="visitors", key="js_confirmed")
        self.assertEqual(row.events, 2)
        # The all-visitors row is still a raw count, beacon included.
        self.assertEqual(
            UsageDaily.objects.get(day=self.today, metric="visitors", key="").events, 3
        )

    def test_confirmed_event_count_excludes_unconfirmed_visitors(self):
        self._event(self.today, visitor="aaa", event="pageview_js")
        self._event(self.today, visitor="aaa", event="pin_click", key="p")
        self._event(self.today, visitor="nojs")     # never ran any JavaScript
        self._event(self.today, visitor="nojs", event="pool_view", key="p")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(
            UsageDaily.objects.get(day=self.today, metric="visitors", key="js_confirmed").events, 1
        )

    def test_run_timestamp_is_recorded_even_with_no_traffic(self):
        """A quiet pass still makes the numbers current, so the clock must advance."""
        self.assertIsNone(UsageRollupState.load().last_run_at)
        call_command("rollup_usage", verbosity=0)
        first = UsageRollupState.load().last_run_at
        self.assertIsNotNone(first)

        call_command("rollup_usage", verbosity=0)
        self.assertGreaterEqual(UsageRollupState.load().last_run_at, first)

    def test_rerunning_does_not_double_count(self):
        self._event(self.today, visitor="aaa")
        call_command("rollup_usage", verbosity=0)
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(
            UsageDaily.objects.get(day=self.today, metric="visitors", key="").visitors, 1
        )

    def test_days_missed_by_an_outage_are_still_rolled_up(self):
        """A cron gap longer than --days must not lose those days silently."""
        stale = self.today - timedelta(days=10)
        self._event(stale, visitor="aaa")
        call_command("rollup_usage", days=3, verbosity=0)
        self.assertTrue(UsageDaily.objects.filter(day=stale, metric="visitors").exists())

    def test_expired_raw_rows_are_pruned_but_totals_survive(self):
        old = self.today - timedelta(days=USAGE_RAW_RETENTION_DAYS + 1)
        self._event(old, visitor="aaa")
        call_command("rollup_usage", verbosity=0)
        self.assertFalse(UsageEvent.objects.filter(day=old).exists())
        self.assertEqual(
            UsageDaily.objects.get(day=old, metric="visitors", key="").visitors, 1
        )

    def test_unaggregated_days_are_never_pruned(self):
        """The prune must not outrun the rollup, even for a day older than retention."""
        old = self.today - timedelta(days=USAGE_RAW_RETENTION_DAYS + 1)
        self._event(old, visitor="aaa")
        call_command("rollup_usage", no_prune=True, verbosity=0)
        UsageDaily.objects.all().delete()          # simulate the rollup never having run
        call_command("rollup_usage", days=1, verbosity=0)
        self.assertTrue(UsageDaily.objects.filter(day=old).exists())


class StatsPageTests(TestCase):
    def test_anonymous_is_bounced_to_the_admin_login(self):
        resp = self.client.get("/stats/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp["Location"])

    def test_logged_in_non_staff_is_bounced_too(self):
        """Being signed in is not enough — the page is staff-only."""
        from django.contrib.auth.models import User
        User.objects.create_user("regular", "r@example.com", "pw")
        self.client.login(username="regular", password="pw")
        resp = self.client.get("/stats/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin/login/", resp["Location"])

    def test_renders_for_staff(self):
        from django.contrib.auth.models import User
        User.objects.create_superuser("admin", "a@example.com", "pw")
        self.client.login(username="admin", password="pw")
        pool = Pool.objects.create(
            ppr_amenity_id="t1", name="Test Pool", address="1 Main St", slug="test-pool"
        )
        # The beacon matters: the breakdowns show confirmed browsers by default, so a
        # visitor with no JavaScript trace would leave the pool tables empty.
        UsageEvent.objects.create(
            day=timezone.localdate(), event="pool_view", key=pool.slug, visitor="aaa",
            ua_family="chrome/150",
        )
        UsageEvent.objects.create(
            day=timezone.localdate(), event="pageview_js", visitor="aaa",
            ua_family="chrome/150",
        )
        call_command("rollup_usage", verbosity=0)
        resp = self.client.get("/stats/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Test Pool")

    def test_the_browsers_table_reports_the_claim(self):
        from django.contrib.auth.models import User
        User.objects.create_superuser("admin7", "a7@example.com", "pw")
        self.client.login(username="admin7", password="pw")
        day = timezone.localdate()
        # Old enough to be worth seeing in the table, but not one of the versions
        # BOT_UA_FAMILIES rejects outright — those never reach a visitor breakdown.
        UsageEvent.objects.create(
            day=day, event="pageview_js", visitor="aaa", ua_family="safari/14"
        )
        call_command("rollup_usage", verbosity=0)
        resp = self.client.get("/stats/")
        self.assertContains(resp, "Browsers")
        self.assertContains(resp, "safari/14")
        # Both tables: the browser on its own, and the version it claimed.
        self.assertContains(resp, "Browser versions")
        self.assertEqual([r["key"] for r in resp.context["browser_families"]], ["safari"])

    def test_interaction_tile_reports_confirmed_browsers_only(self):
        from django.contrib.auth.models import User
        User.objects.create_superuser("admin4", "a4@example.com", "pw")
        self.client.login(username="admin4", password="pw")
        day = timezone.localdate()
        UsageEvent.objects.create(day=day, event="index", visitor="aaa", ua_family="chrome/150")
        UsageEvent.objects.create(day=day, event="pageview_js", visitor="aaa", ua_family="chrome/150")
        UsageEvent.objects.create(day=day, event="filter", visitor="aaa", ua_family="chrome/150")
        UsageEvent.objects.create(day=day, event="index", visitor="nojs", ua_family="chrome/150")
        call_command("rollup_usage", verbosity=0)

        resp = self.client.get("/stats/?days=7")
        self.assertEqual(resp.context["totals"]["confirmed_events"], 2)
        self.assertEqual(resp.context["totals"]["events"], 4)   # every human row

    def test_breakdowns_default_to_confirmed_browsers(self):
        from django.contrib.auth.models import User
        User.objects.create_superuser("admin6", "a6@example.com", "pw")
        self.client.login(username="admin6", password="pw")
        day = timezone.localdate()
        UsageEvent.objects.create(day=day, event="pool_view", key="seen", visitor="js", ua_family="chrome/150")
        UsageEvent.objects.create(day=day, event="pageview_js", visitor="js", ua_family="chrome/150")
        UsageEvent.objects.create(day=day, event="pool_view", key="seen", visitor="nojs", ua_family="chrome/150")
        call_command("rollup_usage", verbosity=0)

        default = self.client.get("/stats/?days=7")
        self.assertEqual(default.context["audience"], "confirmed")
        self.assertEqual(default.context["pool_views"][0]["visitors"], 1)

        wider = self.client.get("/stats/?days=7&audience=human")
        self.assertEqual(wider.context["audience"], "human")
        self.assertEqual(wider.context["pool_views"][0]["visitors"], 2)

    def test_confirmation_rate_by_browser_shows_the_shortfall_and_its_cause(self):
        """The check on the page's own premise: which browsers confirm, and whether
        the ones that don't were crawlers wearing the name."""
        from django.contrib.auth.models import User
        User.objects.create_superuser("admin8", "a8@example.com", "pw")
        self.client.login(username="admin8", password="pw")
        day = timezone.localdate()
        # Chrome: two visitors, both confirmed.
        for n in range(2):
            UsageEvent.objects.create(day=day, event="index", visitor=f"c{n}", ua_family="chrome/150")
            UsageEvent.objects.create(day=day, event="pageview_js", visitor=f"c{n}", ua_family="chrome/150")
        # Safari: two visitors, one confirmed, plus two more from hosting ranges that
        # never ran anything and were filed as robots.
        UsageEvent.objects.create(day=day, event="index", visitor="s1", ua_family="safari/17")
        UsageEvent.objects.create(day=day, event="pageview_js", visitor="s1", ua_family="safari/17")
        UsageEvent.objects.create(day=day, event="index", visitor="s2", ua_family="safari/17")
        for n in range(2):
            UsageEvent.objects.create(day=day, event="index", visitor=f"rack{n}",
                                      ua_family="safari/17", datacenter=True)
        # A family seen only from hosting ranges: nobody to have confirmed, so it has
        # no rate at all rather than a rate of zero.
        UsageEvent.objects.create(day=day, event="index", visitor="rack9",
                                  ua_family="opera/98", datacenter=True)
        call_command("rollup_usage", verbosity=0)

        resp = self.client.get("/stats/?days=7")
        rows = {r["label"]: r for r in resp.context["confirmed_rates"]}
        self.assertIsNone(rows["opera/98"]["rate"])
        self.assertIn('<td class="text-end">&mdash;</td>', resp.content.decode())
        self.assertEqual(rows["chrome/150"]["rate"], 100)
        self.assertEqual(rows["chrome/150"]["datacenter"], 0)
        self.assertEqual(rows["safari/17"]["visitors"], 2)
        self.assertEqual(rows["safari/17"]["rate"], 50)
        # The half that didn't confirm is explained by traffic the rule already
        # rejected, which is the whole reason the column is there.
        self.assertEqual(rows["safari/17"]["datacenter"], 2)

    def test_a_family_the_stale_rule_emptied_still_has_a_row(self):
        """Ranking on surviving visitors would drop it off the table at the exact
        moment the rule started working, taking the evidence with it."""
        from django.contrib.auth.models import User
        User.objects.create_superuser("admin21", "a21@example.com", "pw")
        self.client.login(username="admin21", password="pw")
        day = timezone.localdate()
        for n in range(12):
            UsageEvent.objects.create(day=day, event="index", visitor=f"v{n}", ua_family="chrome/150")
            UsageEvent.objects.create(day=day, event="pageview_js", visitor=f"v{n}", ua_family="chrome/150")
        for n in range(20):
            UsageEvent.objects.create(day=day, event="index", visitor=f"f{n}", ua_family="chrome/142")
        call_command("rollup_usage", verbosity=0)

        resp = self.client.get("/stats/?days=7")
        rows = {r["label"]: r for r in resp.context["confirmed_rates"]}
        self.assertEqual(rows["chrome/142"]["visitors"], 0)
        self.assertEqual(rows["chrome/142"]["stale"], 20)
        self.assertIsNone(rows["chrome/142"]["rate"])
        # Ranked above the browser with real visitors, having claimed more traffic.
        labels = [r["label"] for r in resp.context["confirmed_rates"]]
        self.assertLess(labels.index("chrome/142"), labels.index("chrome/150"))

    def _staff(self, name):
        from django.contrib.auth.models import User
        User.objects.create_superuser(name, f"{name}@example.com", "pw")
        self.client.login(username=name, password="pw")

    def _hour_row(self, day, hour, audience, visitors):
        UsageDaily.objects.create(
            day=day, metric="hour", key=f"{hour:02d}", audience=audience,
            visitors=visitors, events=visitors,
        )

    def test_a_one_day_window_is_charted_by_the_hour(self):
        self._staff("admin9")
        now = timezone.localtime()
        self._hour_row(now.date(), now.hour, "human", 4)
        self._hour_row(now.date(), now.hour, "confirmed", 3)

        hourly = self.client.get("/stats/?days=1").context["hourly"]

        # One bar per hour so far today and none for hours that haven't happened:
        # a run of empty bars to midnight would read as traffic falling off a cliff.
        self.assertEqual(len(hourly["rows"]), now.hour + 1)
        self.assertEqual(hourly["rows"][-1]["visitors"], 4)
        self.assertEqual(hourly["rows"][-1]["confirmed"], 3)
        self.assertEqual(hourly["peak"], 4)
        self.assertFalse(hourly["multiday"])

    def test_a_two_day_window_charts_both_days_by_the_hour(self):
        self._staff("admin10")
        now = timezone.localtime()
        self._hour_row(now.date() - timedelta(days=1), 23, "human", 2)
        self._hour_row(now.date(), now.hour, "human", 5)

        hourly = self.client.get("/stats/?days=2").context["hourly"]

        # All of yesterday, then today up to the hour in progress.
        self.assertEqual(len(hourly["rows"]), 24 + now.hour + 1)
        self.assertEqual(hourly["peak"], 5)
        self.assertTrue(hourly["multiday"])

    def test_longer_windows_keep_the_per_day_chart(self):
        self._staff("admin11")
        now = timezone.localtime()
        self._hour_row(now.date(), now.hour, "human", 4)
        self.assertIsNone(self.client.get("/stats/?days=7").context["hourly"])

    def test_a_window_with_no_hours_recorded_falls_back_to_the_daily_chart(self):
        """Days predating the hour rollup, or a deploy still waiting on
        `rollup_usage --all`. A flat empty chart beside tiles reporting visitors
        would say the site was dead all day, which is the one thing it wasn't."""
        self._staff("admin12")
        day = timezone.localdate()
        UsageEvent.objects.create(day=day, event="index", visitor="aaa", ua_family="chrome/150")
        UsageEvent.objects.create(day=day, event="pageview_js", visitor="aaa", ua_family="chrome/150")
        call_command("rollup_usage", verbosity=0)
        UsageDaily.objects.filter(metric="hour").delete()  # as if rolled up before hours existed

        resp = self.client.get("/stats/?days=2")
        self.assertIsNone(resp.context["hourly"])
        self.assertEqual(resp.context["totals"]["visitors"], 1)

    def test_days_with_nothing_recorded_become_one_break_in_the_chart(self):
        self._staff("admin17")
        today = timezone.localdate()
        for day in (today - timedelta(days=6), today):
            UsageDaily.objects.create(day=day, metric="visitors", key="", visitors=2, events=5)

        resp = self.client.get("/stats/?days=7")
        chart = resp.context["chart"]

        # Five empty days in the middle, drawn once rather than five times over.
        self.assertEqual([c["kind"] for c in chart], ["bar", "break", "bar"])
        self.assertEqual(chart[1]["days"], 5)
        self.assertEqual(chart[1]["from"], today - timedelta(days=5))
        self.assertEqual(chart[1]["to"], today - timedelta(days=1))
        # A break is a statement about us, not a number: it can't reach the totals.
        self.assertEqual(resp.context["totals"]["visitors"], 4)
        self.assertEqual(resp.context["peak_visitors"], 2)

    def test_dates_are_printed_where_measurement_starts_and_stops(self):
        self._staff("admin19")
        today = timezone.localdate()
        for n in range(7):
            UsageDaily.objects.create(
                day=today - timedelta(days=n), metric="visitors", key="", visitors=1, events=1
            )

        chart = self.client.get("/stats/?days=30").context["chart"]

        # The window opens mid-gap, so its own start date names a day nobody recorded
        # anything on. The dates worth printing are the ends of the run beside it.
        self.assertEqual(chart[0]["kind"], "break")
        self.assertEqual(chart[1]["tick"], "start")
        self.assertEqual(chart[1]["day"], today - timedelta(days=6))
        self.assertEqual(chart[-1]["tick"], "end")
        self.assertEqual(chart[-1]["day"], today)

    def test_a_run_too_short_for_two_dates_gets_one(self):
        """Each label is wider than the 10px bar under it, so two on a short run
        would overlap into a smear."""
        self._staff("admin20")
        today = timezone.localdate()
        for n in range(3):
            UsageDaily.objects.create(
                day=today - timedelta(days=n + 5), metric="visitors", key="",
                visitors=1, events=1,
            )

        chart = self.client.get("/stats/?days=30").context["chart"]

        bars = [c for c in chart if c["kind"] == "bar"]
        self.assertEqual([b.get("tick") for b in bars], ["start", None, None])

    def test_a_day_of_nothing_but_robots_is_a_bar_and_not_a_break(self):
        """The distinction the whole thing rests on. Counts were stored, so somebody
        was counting — they just counted nobody worth reporting."""
        self._staff("admin18")
        UsageEvent.objects.create(
            day=timezone.localdate(), event="index", visitor="crawler",
            client_class="bot", ua_family="other",
        )
        call_command("rollup_usage", verbosity=0)

        chart = self.client.get("/stats/?days=1").context["chart"]

        self.assertEqual([c["kind"] for c in chart], ["bar"])
        self.assertEqual(chart[0]["visitors"], 0)

    def test_all_time_reaches_back_to_the_first_day_recorded(self):
        self._staff("admin14")
        today = timezone.localdate()
        UsageDaily.objects.create(
            day=today - timedelta(days=400), metric="visitors", key="", visitors=3, events=3
        )

        resp = self.client.get("/stats/?days=all")

        # Past the 365 the numeric windows are capped at: the cap exists to bound a
        # typo, not to hide a season that has already been recorded.
        self.assertEqual(resp.context["days"], 401)
        self.assertTrue(resp.context["all_time"])
        self.assertEqual(resp.context["first_day"], today - timedelta(days=400))

    def test_all_time_survives_switching_audience(self):
        """The audience form resubmits the window; a resolved day count would freeze
        it at today's length and quietly stop growing tomorrow."""
        self._staff("admin15")
        UsageDaily.objects.create(
            day=timezone.localdate() - timedelta(days=5), metric="visitors", key="",
            visitors=1, events=1,
        )
        html = self.client.get("/stats/?days=all").content.decode()
        self.assertIn('<input type="hidden" name="days" value="all">', html)

    def test_all_time_with_nothing_recorded_does_not_break(self):
        self._staff("admin16")
        resp = self.client.get("/stats/?days=all")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["days"], 1)

    def test_unknown_audience_falls_back_to_confirmed(self):
        from django.contrib.auth.models import User
        User.objects.create_superuser("admin7", "a7@example.com", "pw")
        self.client.login(username="admin7", password="pw")
        resp = self.client.get("/stats/?audience=everyone")
        self.assertEqual(resp.context["audience"], "confirmed")

    def test_interaction_types_chart_leaves_out_the_beacon(self):
        from django.contrib.auth.models import User
        User.objects.create_superuser("admin5", "a5@example.com", "pw")
        self.client.login(username="admin5", password="pw")
        day = timezone.localdate()
        for n in range(5):
            UsageEvent.objects.create(day=day, event="pageview_js", visitor=f"v{n}", ua_family="chrome/150")
            UsageEvent.objects.create(day=day, event="index", visitor=f"v{n}", ua_family="chrome/150")
        call_command("rollup_usage", verbosity=0)

        resp = self.client.get("/stats/?days=7")
        keys = [r["key"] for r in resp.context["events_by_type"]]
        self.assertNotIn("pageview_js", keys)
        self.assertIn("index", keys)
        # Dropped from the chart only — the stored count is still there.
        self.assertTrue(
            UsageDaily.objects.filter(day=day, metric="event", key="pageview_js").exists()
        )

    def test_one_page_visits_are_collapsed_above_and_split_below(self):
        """The main journey panel shows one passive-visit row; the new panel breaks it down."""
        from django.contrib.auth.models import User
        User.objects.create_superuser("admin8", "a8@example.com", "pw")
        self.client.login(username="admin8", password="pw")
        day = timezone.localdate()
        UsageEvent.objects.create(day=day, event="index", visitor="lister", ua_family="chrome/150")
        UsageEvent.objects.create(day=day, event="pageview_js", visitor="lister", ua_family="chrome/150")
        UsageEvent.objects.create(day=day, event="pool_view", key="p", visitor="detailer", ua_family="chrome/150")
        UsageEvent.objects.create(day=day, event="pageview_js", visitor="detailer", ua_family="chrome/150")
        call_command("rollup_usage", verbosity=0)

        resp = self.client.get("/stats/?days=7")

        journey_labels = [row["label"] for row in resp.context["journeys"]]
        self.assertEqual(journey_labels, [
            "Looked at more than one page", "One page, but used it", "One page, then left",
        ])
        passive_row = resp.context["journeys"][-1]
        self.assertEqual(passive_row["visitors"], 2)

        breakdown = {row["label"]: row["visitors"] for row in resp.context["one_page_breakdown"]}
        self.assertEqual(breakdown, {
            "Pool list page": 1, "Pool detail page": 1, "Other/unknown": 0,
        })
        self.assertEqual(resp.context["one_page_total"], 2)
        self.assertContains(resp, "One-page, no-interaction visits")
        self.assertContains(resp, "Confirmed browsers only")

    def test_collection_start_shows_only_when_the_window_reaches_it(self):
        from django.contrib.auth.models import User
        User.objects.create_superuser("admin3", "a3@example.com", "pw")
        self.client.login(username="admin3", password="pw")
        began = timezone.localdate() - timedelta(days=5)
        UsageEvent.objects.create(day=began, event="index", visitor="aaa", ua_family="chrome/150")
        call_command("rollup_usage", all=True, verbosity=0)

        # A 30-day window reaches past the first day recorded, so say where it starts.
        reaching = self.client.get("/stats/?days=30")
        self.assertEqual(reaching.context["collection_start"], began)

        # A 3-day window sits entirely inside the collected period; the note would mislead.
        inside = self.client.get("/stats/?days=3")
        self.assertIsNone(inside.context["collection_start"])

    def test_today_window_renders_a_single_day(self):
        from django.contrib.auth.models import User
        User.objects.create_superuser("admin2", "a2@example.com", "pw")
        self.client.login(username="admin2", password="pw")
        UsageEvent.objects.create(
            day=timezone.localdate() - timedelta(days=3), event="index", visitor="old",
            ua_family="chrome/150",
        )
        UsageEvent.objects.create(
            day=timezone.localdate(), event="index", visitor="new", ua_family="chrome/150"
        )
        call_command("rollup_usage", verbosity=0)

        resp = self.client.get("/stats/?days=1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["series"]), 1)
        self.assertEqual(resp.context["totals"]["visitors"], 1)
        self.assertEqual(resp.context["first_day"], timezone.localdate())


class RenderStaticSiteTests(TestCase):
    """The offseason snapshot is rendered once and served unchanged for ~8 months,
    so the things that must not regress are: no date-relative content gets frozen
    into it, the URLs match what the live sitemap indexed, and pools whose
    schedule never made it into PoolSeasonHistory still show their hours."""

    def setUp(self):
        self.out = Path(tempfile.mkdtemp()) / "build"
        self.assets = Path(tempfile.mkdtemp()) / "assets"
        self.assets.mkdir(parents=True)
        (self.assets / "pic.png").write_bytes(b"png")
        (self.assets / "_redirects").write_text("/submit  /  302\n")

    def _render(self, season_year=2026):
        call_command(
            "render_static_site",
            season_year=season_year,
            out=str(self.out),
            assets=str(self.assets),
            verbosity=0,
        )

    def _make_pool(self, name="Awbury Pool", **kwargs):
        defaults = dict(ppr_amenity_id=name.lower().replace(" ", "-"), address="1 Main St")
        return Pool.objects.create(name=name, **{**defaults, **kwargs})

    def test_pool_page_is_written_at_its_live_url(self):
        pool = self._make_pool(opening_date=date(2026, 6, 17), closing_date=date(2026, 8, 30))
        self._render()
        page = self.out / "pools" / pool.slug / "index.html"
        self.assertTrue(page.exists())
        html = page.read_text()
        # Same path the live site indexed, so the URL survives the cutover.
        self.assertIn(f'<link rel="canonical" href="https://phillypools.app{pool.get_absolute_url()}"', html)
        self.assertIn("June 17, 2026", html)
        self.assertIn("August 30, 2026", html)
        self.assertIn("10 weeks, 5 days", html)

    def test_schedule_falls_back_to_live_fields_when_history_row_is_missing(self):
        # _upsert_season_history only creates a row when the pool has dates, so a
        # schedule-only pool has no 2026 history at all. Without the fallback its
        # page would render the generic-hours copy and lose real information.
        pool = self._make_pool(weekday_schedule="11-7 open swim", weekend_schedule="12-5")
        self.assertFalse(pool.season_history.exists())
        self._render()
        html = (self.out / "pools" / pool.slug / "index.html").read_text()
        self.assertIn("11-7 open swim", html)
        self.assertIn("12-5", html)

    def test_history_row_wins_over_live_fields(self):
        pool = self._make_pool(
            opening_date=date(2026, 6, 17), weekday_schedule="live text",
        )
        pool.season_history.filter(year=2026).update(weekday_schedule="archived text")
        self._render()
        html = (self.out / "pools" / pool.slug / "index.html").read_text()
        self.assertIn("archived text", html)
        self.assertNotIn("live text", html)

    def test_prior_season_dates_are_not_relabeled_as_the_rendered_season(self):
        # A pool still carrying 2025 dates must not have them presented as 2026's.
        pool = self._make_pool(opening_date=date(2025, 6, 20))
        self._render(season_year=2026)
        html = (self.out / "pools" / pool.slug / "index.html").read_text()
        self.assertIn("2026 season", html)
        self.assertNotIn("June 20, 2025", html)

    def test_nothing_date_relative_is_baked_into_the_page(self):
        pool = self._make_pool(opening_date=date(2026, 6, 17), closing_date=date(2026, 8, 30))
        self._render()
        html = (self.out / "pools" / pool.slug / "index.html").read_text()
        for leaked in ("Closed for the season", "page-loaded", "csrfmiddlewaretoken", "like-btn"):
            self.assertNotIn(leaked, html)

    def test_inactive_pools_get_a_page_and_a_sitemap_entry(self):
        # is_active only means we don't expect an opening date. The page still has an
        # address and notes, and people search for these pools by name, so they're
        # rendered and advertised like any other.
        active = self._make_pool("Active Pool")
        inactive = self._make_pool("Inactive Pool", is_active=False)
        self._render()
        self.assertTrue((self.out / "pools" / active.slug / "index.html").exists())
        self.assertTrue((self.out / "pools" / inactive.slug / "index.html").exists())
        sitemap = (self.out / "sitemap.xml").read_text()
        self.assertIn(active.get_absolute_url(), sitemap)
        self.assertIn(inactive.get_absolute_url(), sitemap)

    def test_pool_that_did_not_open_says_so_in_the_past_tense(self):
        pool = self._make_pool("Shuttered Pool", is_active=False)
        self._render()
        html = (self.out / "pools" / pool.slug / "index.html").read_text()
        self.assertIn("Did not open", html)
        self.assertIn("did not open in 2026", html)
        # Nothing may claim anything about the season that hasn't happened yet.
        self.assertNotIn("expected to open", html)
        # Generic citywide hours would imply this pool kept them.
        self.assertNotIn("11–7 on weekdays", html)

    def test_inactive_pool_that_did_open_reads_as_a_normal_season(self):
        # Deactivated after the fact — it did open, so past-tense "did not open" is wrong.
        pool = self._make_pool(
            "Late Closure Pool", is_active=False,
            opening_date=date(2026, 6, 17), closing_date=date(2026, 7, 1),
        )
        self._render()
        html = (self.out / "pools" / pool.slug / "index.html").read_text()
        self.assertNotIn("Did not open", html)
        self.assertIn("June 17, 2026", html)

    def test_sitemap_lists_the_homepage_and_every_pool(self):
        for name in ("A Pool", "B Pool"):
            self._make_pool(name)
        self._make_pool("Inactive Pool", is_active=False)
        self._render()
        sitemap = (self.out / "sitemap.xml").read_text()
        minidom.parseString(sitemap)  # must be well-formed or GSC rejects it
        self.assertEqual(sitemap.count("<loc>"), 4)
        self.assertIn("<loc>https://phillypools.app/</loc>", sitemap)

    def test_index_links_every_pool_so_the_pages_are_not_orphans(self):
        pools = [self._make_pool(n) for n in ("A Pool", "B Pool", "C Pool")]
        pools.append(self._make_pool("D Pool", is_active=False))
        self._render()
        index = (self.out / "index.html").read_text()
        # Inactive pools included: they have pages now, and an unlinked page is
        # reachable only via the sitemap it's deliberately absent from.
        for pool in pools:
            self.assertIn(f'href="{pool.get_absolute_url()}"', index)
        self.assertIn("Hope you had a great summer!", index)

    def test_assets_and_generated_files_land_in_the_output(self):
        self._make_pool()
        self._render()
        for name in ("pic.png", "_redirects", "index.html", "404.html", "robots.txt", "sitemap.xml"):
            self.assertTrue((self.out / name).exists(), name)
        # A /pools/* redirect would shadow every archived page Pages serves.
        self.assertNotIn("/pools/*", (self.out / "_redirects").read_text())

    def test_refuses_to_render_an_empty_site(self):
        with self.assertRaises(CommandError):
            self._render()

    def test_stale_output_is_cleared_so_removed_pools_do_not_linger(self):
        pool = self._make_pool()
        self._render()
        stale = self.out / "pools" / "gone-pool"
        stale.mkdir(parents=True)
        (stale / "index.html").write_text("old")
        self._render()
        self.assertFalse(stale.exists())
        self.assertTrue((self.out / "pools" / pool.slug / "index.html").exists())


class NearbyPoolsTests(TestCase):
    """The heading has to describe what the box is actually offering, and the
    in-season list has to contain only pools someone could swim in today."""

    def setUp(self):
        self.today = date(2026, 7, 15)
        # Roughly a mile apart each, north from a fixed point in Philadelphia.
        self.pools = {}
        for i, name in enumerate(["Home", "One", "Two", "Three", "Four"]):
            self.pools[name] = Pool.objects.create(
                name=f"{name} Pool", ppr_amenity_id=f"id-{i}", address=f"{i} Main St",
                latitude=39.95 + i * 0.0145, longitude=-75.16,
                opening_date=date(2026, 6, 1), closing_date=date(2026, 8, 31),
            )

    def _context(self, pool, offseason=False):
        return nearby_pools_context(pool, list(Pool.objects.all()), self.today, offseason=offseason)

    def test_open_pool_gets_other_pools_heading(self):
        ctx = self._context(self.pools["Home"])
        self.assertEqual(ctx["nearby_heading"], "Closest other pools")

    def test_closed_pool_gets_open_pools_heading(self):
        home = self.pools["Home"]
        home.closing_date = date(2026, 7, 1)  # season already over for this pool
        home.save()
        ctx = self._context(home)
        self.assertEqual(ctx["nearby_heading"], "Closest open pools")

    def test_inactive_pool_gets_open_pools_heading(self):
        home = self.pools["Home"]
        home.is_active = False
        home.save()
        self.assertEqual(self._context(home)["nearby_heading"], "Closest open pools")

    def test_pool_closed_by_schedule_change_is_not_treated_as_open(self):
        home = self.pools["Home"]
        ScheduleChange.objects.create(
            pool=home, date_from=self.today, date_to=self.today, description="Closed today"
        )
        # Heading reflects that this pool isn't usable today...
        self.assertEqual(self._context(home)["nearby_heading"], "Closest open pools")
        # ...and it's excluded from its neighbours' lists for the same reason.
        listed = [p.name for p, _ in self._context(self.pools["One"])["nearby_pools"]]
        self.assertNotIn("Home Pool", listed)

    def test_returns_three_nearest_in_ascending_distance(self):
        ctx = self._context(self.pools["Home"])
        names = [p.name for p, _ in ctx["nearby_pools"]]
        self.assertEqual(names, ["One Pool", "Two Pool", "Three Pool"])
        distances = [m for _, m in ctx["nearby_pools"]]
        self.assertEqual(distances, sorted(distances))
        self.assertAlmostEqual(distances[0], 1.0, places=1)

    def test_the_pool_itself_is_never_listed(self):
        for name in self.pools:
            listed = [p.pk for p, _ in self._context(self.pools[name])["nearby_pools"]]
            self.assertNotIn(self.pools[name].pk, listed)

    def test_degrades_when_fewer_than_three_pools_are_open(self):
        Pool.objects.exclude(pk__in=[self.pools["Home"].pk, self.pools["One"].pk]).delete()
        ctx = self._context(self.pools["Home"])
        self.assertEqual([p.name for p, _ in ctx["nearby_pools"]], ["One Pool"])

    def test_sole_open_pool_says_so_instead_of_listing_nothing(self):
        Pool.objects.exclude(pk=self.pools["Home"].pk).update(is_active=False)
        ctx = self._context(self.pools["Home"])
        self.assertEqual(ctx["nearby_pools"], [])
        self.assertTrue(ctx["nearby_only_open"])

    def test_links_carry_the_hooks_the_click_beacon_reads(self):
        """
        The beacon in detail.html finds these by id and data-slug. Nothing else on
        the page would break if they were dropped in a refactor — the box would keep
        working and simply stop being measurable — so the contract is asserted here.
        """
        from django.template.loader import render_to_string

        html = render_to_string("pools/_nearby_pools.html", self._context(self.pools["Home"]))
        self.assertIn('id="nearby-pools"', html)
        for name in ["One", "Two", "Three"]:
            self.assertIn(f'data-slug="{self.pools[name].slug}"', html)

    def test_closed_pools_are_excluded_in_season(self):
        Pool.objects.exclude(pk=self.pools["Home"].pk).update(is_active=False)
        far = Pool.objects.create(
            name="Far Pool", ppr_amenity_id="far", address="9 Main St",
            latitude=40.10, longitude=-75.16,
            opening_date=date(2026, 6, 1), closing_date=date(2026, 8, 31),
        )
        ctx = self._context(self.pools["Home"])
        # The only open pool is the distant one; nearer pools are shut.
        self.assertEqual([p.name for p, _ in ctx["nearby_pools"]], [far.name])

    def test_offseason_lists_pools_regardless_of_status(self):
        Pool.objects.exclude(pk=self.pools["Home"].pk).update(is_active=False)
        ctx = self._context(self.pools["Home"], offseason=True)
        self.assertEqual(ctx["nearby_heading"], "Closest pools")
        self.assertEqual(
            [p.name for p, _ in ctx["nearby_pools"]],
            ["One Pool", "Two Pool", "Three Pool"],
        )

    def test_pools_without_coordinates_are_skipped(self):
        Pool.objects.filter(pk=self.pools["One"].pk).update(latitude=None, longitude=None)
        ctx = self._context(self.pools["Home"])
        self.assertNotIn("One Pool", [p.name for p, _ in ctx["nearby_pools"]])

    def test_no_box_for_a_pool_without_coordinates(self):
        home = self.pools["Home"]
        Pool.objects.filter(pk=home.pk).update(latitude=None, longitude=None)
        home.refresh_from_db()
        self.assertEqual(self._context(home)["nearby_pools"], [])

    def test_detail_page_renders_the_box_with_linked_names(self):
        resp = self.client.get(self.pools["Home"].get_absolute_url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Closest other pools")
        self.assertContains(resp, f'href="{self.pools["One"].get_absolute_url()}"')

    def test_page_shows_the_sole_open_pool_message(self):
        Pool.objects.exclude(pk=self.pools["Home"].pk).update(is_active=False)
        resp = self.client.get(self.pools["Home"].get_absolute_url())
        self.assertContains(resp, "Closest other pools")
        self.assertContains(resp, "This is the only pool open today.")


class NearbyPoolsScarcityTests(TestCase):
    """What the box does when almost nothing is open — the cases where the
    heading and the emptiness rule interact."""

    def setUp(self):
        self.today = date(2026, 7, 15)
        self.a, self.b, self.c = [
            Pool.objects.create(
                name=f"{n} Pool", ppr_amenity_id=f"id-{n}", address=f"{i} Main St",
                latitude=39.95 + i * 0.0145, longitude=-75.16,
            )
            for i, n in enumerate(["A", "B", "C"])
        ]

    def _open(self, *pools):
        for pool in pools:
            pool.opening_date = date(2026, 6, 1)
            pool.closing_date = date(2026, 8, 31)
            pool.save()

    def _ctx(self, pool):
        return nearby_pools_context(pool, list(Pool.objects.all()), self.today)

    def test_viewing_the_only_open_pool_says_so(self):
        self._open(self.a)
        ctx = self._ctx(self.a)
        # Heading is not singularized; the body carries the meaning.
        self.assertEqual(ctx["nearby_heading"], "Closest other pools")
        self.assertTrue(ctx["nearby_only_open"])
        self.assertEqual(ctx["nearby_pools"], [])
        resp = self.client.get(self.a.get_absolute_url())
        self.assertContains(resp, "Closest other pools")
        self.assertContains(resp, "This is the only pool open today.")

    def test_one_open_pool_elsewhere_is_listed_with_thats_it(self):
        self._open(self.c)
        ctx = self._ctx(self.a)
        self.assertEqual(ctx["nearby_heading"], "Closest open pools")
        self.assertEqual([p.name for p, _ in ctx["nearby_pools"]], ["C Pool"])
        self.assertTrue(ctx["nearby_exhaustive"])
        resp = self.client.get(self.a.get_absolute_url())
        self.assertContains(resp, f'href="{self.c.get_absolute_url()}"')
        self.assertContains(resp, "and that's it!")

    def test_two_open_pools_viewing_one_lists_just_the_other(self):
        self._open(self.a, self.b)
        ctx = self._ctx(self.a)
        self.assertEqual(ctx["nearby_heading"], "Closest other pools")
        self.assertEqual([p.name for p, _ in ctx["nearby_pools"]], ["B Pool"])
        self.assertTrue(ctx["nearby_exhaustive"])

    def test_two_open_pools_viewing_a_closed_one_lists_both(self):
        self._open(self.a, self.b)
        ctx = self._ctx(self.c)
        self.assertEqual(ctx["nearby_heading"], "Closest open pools")
        self.assertEqual([p.name for p, _ in ctx["nearby_pools"]], ["B Pool", "A Pool"])
        self.assertTrue(ctx["nearby_exhaustive"])

    def test_three_listed_pools_get_no_thats_it(self):
        d = Pool.objects.create(
            name="D Pool", ppr_amenity_id="id-D", address="9 Main St",
            latitude=40.10, longitude=-75.16,
        )
        self._open(self.a, self.b, self.c, d)
        ctx = self._ctx(self.a)
        self.assertEqual(len(ctx["nearby_pools"]), 3)
        self.assertFalse(ctx["nearby_exhaustive"])
        resp = self.client.get(self.a.get_absolute_url())
        self.assertNotContains(resp, "and that's it!")

    def test_all_pools_closed_falls_back_to_offseason_presentation(self):
        # No pool has dates, so nothing is open. "Closest open pools" has no answer.
        ctx = self._ctx(self.a)
        self.assertEqual(ctx["nearby_heading"], "Closest pools")
        self.assertEqual([p.name for p, _ in ctx["nearby_pools"]], ["B Pool", "C Pool"])
        self.assertFalse(ctx["nearby_only_open"])

    def test_all_pools_closed_lists_inactive_pools_too(self):
        Pool.objects.filter(pk=self.b.pk).update(is_active=False)
        ctx = self._ctx(self.a)
        self.assertEqual(ctx["nearby_heading"], "Closest pools")
        self.assertIn("B Pool", [p.name for p, _ in ctx["nearby_pools"]])


class MonitoredPageCheckTests(TestCase):
    """A monitored page is checked the moment it's created, not on the next cron run."""

    URL = "https://example.com/pool-info"

    def _response(self, body):
        resp = type("R", (), {"text": f"<html><body>{body}</body></html>"})()
        resp.raise_for_status = lambda: None
        return resp

    @patch("pools.services.page_monitor.requests.get")
    def test_start_monitoring_records_baseline_without_a_submission(self, mock_get):
        mock_get.return_value = self._response("Opens June 15")
        page, created, report = start_monitoring(self.URL)
        self.assertTrue(created)
        self.assertTrue(page.content_hash)
        self.assertIsNotNone(page.last_checked)
        self.assertEqual(report.errors, [])
        # The baseline itself is not a change, so it must not queue a submission.
        self.assertFalse(Submission.objects.filter(url=self.URL).exists())

    @patch("pools.services.page_monitor.requests.get")
    def test_start_monitoring_is_idempotent(self, mock_get):
        mock_get.return_value = self._response("Opens June 15")
        start_monitoring(self.URL)
        page, created, report = start_monitoring(self.URL)
        self.assertFalse(created)
        self.assertIsNone(report)
        self.assertEqual(MonitoredPage.objects.filter(url=self.URL).count(), 1)
        self.assertEqual(mock_get.call_count, 1)

    @patch("pools.services.page_monitor.requests.get")
    def test_fetch_failure_still_leaves_the_page_monitored(self, mock_get):
        mock_get.side_effect = Exception("boom")
        page, created, report = start_monitoring(self.URL)
        self.assertTrue(created)
        self.assertEqual(page.content_hash, "")
        self.assertTrue(report.errors)

    @patch("pools.services.page_monitor._create_submission")
    @patch("pools.services.page_monitor.requests.get")
    def test_later_change_creates_a_submission(self, mock_get, mock_create):
        mock_get.return_value = self._response("Opens June 15")
        page, _, _ = start_monitoring(self.URL)
        mock_get.return_value = self._response("Opens June 22")
        report = check_pool_info_page(page)
        self.assertTrue(report.highlights)
        mock_create.assert_called_once_with(self.URL)


class MonitorFromApplyPageTests(TestCase):
    """The multi-pool apply page can start monitoring the source URL."""

    URL = "https://example.com/all-pools"

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_superuser("mod", "mod@example.com", "pw")
        self.client.force_login(self.user)
        self.submission = Submission.objects.create(url=self.URL)
        self.pool = Pool.objects.create(name="A Pool", ppr_amenity_id="id-A", address="1 Main St")

    def _apply(self, **extra):
        return self.client.post(
            reverse("admin:pools_submission_reparse", args=[self.submission.pk]),
            {"action": "apply", "pool_ids": [str(self.pool.pk)],
             f"opening_date_{self.pool.pk}": "2026-06-15", **extra},
        )

    @patch("pools.services.page_monitor.requests.get")
    def test_checkbox_starts_monitoring_and_checks_immediately(self, mock_get):
        resp = type("R", (), {"text": "<html><body>Opens June 15</body></html>"})()
        resp.raise_for_status = lambda: None
        mock_get.return_value = resp

        self._apply(monitor_page="on")

        page = MonitoredPage.objects.get(url=self.URL)
        self.assertEqual(page.page_type, "pool_info")
        self.assertTrue(page.content_hash)
        self.assertIsNotNone(page.last_checked)
        self.pool.refresh_from_db()
        self.assertEqual(self.pool.opening_date, date(2026, 6, 15))

    @patch("pools.services.page_monitor.requests.get")
    def test_unchecked_box_leaves_the_page_unmonitored(self, mock_get):
        self._apply()
        self.assertFalse(MonitoredPage.objects.filter(url=self.URL).exists())
        mock_get.assert_not_called()

    def _reparse_page(self):
        result = {"pool_id": self.pool.pk, "pool_name": self.pool.name,
                  "opening_date": "2026-06-15", "closing_date": None,
                  "phone_number": "", "notes": ""}
        with patch("pools.services.llm_parser.build_pool_list", return_value=[]), \
             patch("pools.services.url_fetcher.fetch_url", return_value="page text"), \
             patch("pools.services.llm_parser.parse_all_pools", return_value=[result]):
            return self.client.get(
                reverse("admin:pools_submission_reparse", args=[self.submission.pk])
            )

    def test_apply_page_offers_the_checkbox(self):
        self.assertContains(self._reparse_page(), 'name="monitor_page"')

    def test_already_monitored_url_offers_no_checkbox(self):
        MonitoredPage.objects.create(url=self.URL, content_hash="abc")
        resp = self._reparse_page()
        self.assertNotContains(resp, 'name="monitor_page"')
        self.assertContains(resp, "Already monitored")


class PoolGisCheckTests(TestCase):
    """`check_pool_gis` turns disagreements between the city's GIS layer and our data
    into reviewable submissions — once each, not once per cron run."""

    def setUp(self):
        self.pool = Pool.objects.create(
            name="Dendy Pool",
            slug="dendy-pool",
            address="1501-39 N. 10th St., 19122",
            ppr_amenity_id="P0100-S0000-B0000-A0001",
            opening_date=date(2026, 7, 5),
        )

    def _attrs(self, open_date="2026-07-01", status="ACTIVE", amenity_id=None):
        ms = None
        if open_date:
            d = date.fromisoformat(open_date)
            ms = int(datetime(d.year, d.month, d.day, tzinfo=utc_tz.utc).timestamp() * 1000)
        return {
            "ppr_amenity_id": amenity_id or self.pool.ppr_amenity_id,
            "pool_name": "Dendy Pool",
            "pool_status": status,
            "pool_open_date": ms,
        }

    def _run(self, attrs_list, fields=None, **kwargs):
        from pools.services.gis import check_pool_gis
        with patch("pools.services.gis.fetch_features", return_value=attrs_list), \
             patch("pools.services.gis.fetch_layer_field_names", return_value=fields or ["pool_open_date"]):
            return check_pool_gis(**kwargs)

    def test_differing_date_creates_a_submission(self):
        report = self._run([self._attrs()])
        self.assertTrue(report.highlights)
        sub = Submission.objects.get()
        self.assertEqual(sub.parsed_pool, self.pool)
        self.assertEqual(sub.parsed_opening_date, date(2026, 7, 1))
        self.assertEqual(sub.status, "pending")
        # Applying it must cite something a visitor can read, not a JSON endpoint.
        self.assertIn("opendataphilly.org", sub.url)

    def test_same_value_is_not_proposed_twice(self):
        self._run([self._attrs()])
        self._run([self._attrs()])
        self.assertEqual(Submission.objects.count(), 1)

    def test_new_gis_value_is_proposed_again(self):
        self._run([self._attrs()])
        self._run([self._attrs(open_date="2026-07-09")])
        self.assertEqual(Submission.objects.count(), 2)
        self.assertEqual(
            Submission.objects.order_by("-id").first().parsed_opening_date,
            date(2026, 7, 9),
        )

    def test_agreement_proposes_nothing(self):
        report = self._run([self._attrs(open_date="2026-07-05")])
        self.assertFalse(Submission.objects.exists())
        self.assertFalse(report.highlights)

    def test_inactive_record_is_recorded_but_not_proposed(self):
        from pools.models import PoolGisState
        self._run([self._attrs(status="INACTIVE")])
        self.assertFalse(Submission.objects.exists())
        state = PoolGisState.objects.get(pool=self.pool)
        self.assertEqual(state.gis_status, "INACTIVE")
        self.assertEqual(state.gis_opening_date, date(2026, 7, 1))

    def test_pool_without_amenity_id_is_skipped(self):
        Pool.objects.create(
            name="Lincoln Indoor Pool", slug="lincoln-pool",
            address="3201 Ryan Ave., 19136", ppr_amenity_id="",
        )
        report = self._run([self._attrs()])
        self.assertTrue(any("no ppr_amenity_id" in n for n in report.notes))

    def test_new_closing_date_field_is_flagged(self):
        report = self._run(
            [self._attrs(open_date="2026-07-05")],
            fields=["pool_open_date", "pool_close_date"],
        )
        self.assertTrue(any("pool_close_date" in h for h in report.highlights))

    def test_dry_run_writes_nothing(self):
        from pools.models import PoolGisState
        report = self._run([self._attrs()], dry_run=True)
        self.assertTrue(report.highlights)
        self.assertFalse(Submission.objects.exists())
        self.assertFalse(PoolGisState.objects.exists())

    def test_fetch_failure_is_reported_not_raised(self):
        from pools.services.gis import check_pool_gis
        with patch("pools.services.gis.fetch_features", side_effect=RuntimeError("boom")):
            report = check_pool_gis()
        self.assertTrue(report.errors)
        self.assertFalse(Submission.objects.exists())

    def test_epoch_conversion_does_not_shift_a_day(self):
        from pools.services.gis import epoch_ms_to_date
        # Amos Pool's real value from the layer: UTC midnight on 2026-07-31.
        self.assertEqual(epoch_ms_to_date(1785456000000), date(2026, 7, 31))


class SubmissionRawContentAdminTests(TestCase):
    """The fetched source record has to be visible to the reviewer. GIS submissions
    carry the whole city record there and no LLM response, so if this field isn't
    rendered there is nothing to judge the proposal against."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_superuser("admin2", "a2@example.com", "pw")
        self.client.force_login(self.user)
        self.pool = Pool.objects.create(
            name="Dendy Pool", slug="dendy-pool-2",
            address="1501 N. 10th St., 19122",
            ppr_amenity_id="P0999-S0000-B0000-A0001",
            opening_date=date(2026, 7, 5),
        )

    def test_fetched_content_is_rendered_on_the_change_page(self):
        sub = Submission.objects.create(
            url="https://opendataphilly.org/datasets/ppr-swimming-pools/",
            parsed_pool=self.pool,
            parsed_opening_date=date(2026, 7, 1),
            raw_fetched_content='{"pool_status": "ACTIVE", "comments": "Renovated (2025)."}',
        )
        resp = self.client.get(
            reverse("admin:pools_submission_change", args=[sub.pk])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Renovated (2025)")
        self.assertContains(resp, "Fetched source content")

    def test_blank_content_renders_a_dash_not_an_error(self):
        sub = Submission.objects.create(url="https://example.com/x", parsed_pool=self.pool)
        resp = self.client.get(
            reverse("admin:pools_submission_change", args=[sub.pk])
        )
        self.assertEqual(resp.status_code, 200)
