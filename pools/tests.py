from datetime import timedelta

from django.core.management import call_command
from django.test import RequestFactory, TestCase
from django.utils import timezone

from pools.models import Pool, UsageDaily, UsageEvent, VisitorSalt
from pools.services import usage
from pools.services.usage import USAGE_RAW_RETENTION_DAYS


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


class PinClickTests(TestCase):
    def setUp(self):
        _reset_salt_cache()
        self.pool = Pool.objects.create(
            ppr_amenity_id="t1", name="Test Pool", address="1 Main St", slug="test-pool"
        )

    def test_click_is_recorded(self):
        resp = self.client.post("/pin-click/", {"slug": "test-pool"})
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(UsageEvent.objects.filter(event="pin_click", key="test-pool").count(), 1)

    def test_unknown_slug_is_ignored_without_error(self):
        resp = self.client.post("/pin-click/", {"slug": "no-such-pool"})
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(UsageEvent.objects.count(), 0)

    def test_get_is_rejected(self):
        self.assertEqual(self.client.get("/pin-click/").status_code, 405)

    def test_rate_limit_caps_a_single_visitor(self):
        from pools.views import PIN_CLICK_DAILY_MAX
        day = timezone.localdate()
        visitor = usage.visitor_hash(_fake_request(ip="127.0.0.1", ua=""), day)
        UsageEvent.objects.bulk_create([
            UsageEvent(day=day, event="pin_click", key="test-pool", visitor=visitor)
            for _ in range(PIN_CLICK_DAILY_MAX)
        ])
        resp = self.client.post("/pin-click/", {"slug": "test-pool"})
        self.assertEqual(resp.status_code, 429)


class RollupTests(TestCase):
    def setUp(self):
        _reset_salt_cache()
        self.today = timezone.localdate()

    def _event(self, day, event="index", visitor="aaa", client_class="unknown", **kw):
        return UsageEvent.objects.create(
            day=day, event=event, visitor=visitor, client_class=client_class, **kw
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
        UsageEvent.objects.create(
            day=timezone.localdate(), event="pool_view", key=pool.slug, visitor="aaa"
        )
        call_command("rollup_usage", verbosity=0)
        resp = self.client.get("/stats/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Test Pool")
