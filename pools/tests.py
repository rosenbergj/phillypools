from datetime import timedelta

from django.core.management import call_command
from django.test import RequestFactory, TestCase
from django.utils import timezone

from pools.models import Pool, UsageDaily, UsageEvent, UsageRollupState, VisitorSalt
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


class PoolClickTests(TestCase):
    """Both popup beacons: the pin on the map and the entry in the list."""

    # (endpoint, event recorded) — every case below runs against both.
    ROUTES = [("/pin-click/", "pin_click"), ("/card-click/", "card_click")]

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

    def test_the_two_routes_stay_separate(self):
        """Same pool, same popup, but the map and the list must remain tellable apart."""
        self.client.post("/pin-click/", {"slug": "test-pool"})
        self.client.post("/card-click/", {"slug": "test-pool"})
        self.assertEqual(UsageEvent.objects.filter(event="pin_click").count(), 1)
        self.assertEqual(UsageEvent.objects.filter(event="card_click").count(), 1)

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

    def _journey(self, key):
        return UsageDaily.objects.get(day=self.today, metric="journey", key=key).visitors

    def test_journeys_split_confirmed_browsers_three_ways(self):
        # Two pages, so multi-page whether or not they touched anything.
        self._event(self.today, visitor="multi")
        self._event(self.today, visitor="multi", event="pageview_js")
        self._event(self.today, visitor="multi", event="pool_view", key="p")
        # One page, but clicked a pin.
        self._event(self.today, visitor="engaged")
        self._event(self.today, visitor="engaged", event="pin_click", key="p")
        # One page, beacon only.
        self._event(self.today, visitor="passive")
        self._event(self.today, visitor="passive", event="pageview_js")

        call_command("rollup_usage", verbosity=0)

        self.assertEqual(self._journey("multi_page"), 1)
        self.assertEqual(self._journey("single_engaged"), 1)
        self.assertEqual(self._journey("single_passive"), 1)

    def test_journeys_add_back_up_to_the_confirmed_total(self):
        """The three buckets partition confirmed browsers — no one counted twice or lost."""
        self._event(self.today, visitor="aaa", event="pageview_js")
        self._event(self.today, visitor="bbb", event="filter")
        self._event(self.today, visitor="ccc", event="pageview_js")
        self._event(self.today, visitor="ccc", event="pool_view", key="p")

        call_command("rollup_usage", verbosity=0)

        confirmed = UsageDaily.objects.get(
            day=self.today, metric="visitors", key="js_confirmed"
        ).visitors
        total = sum(
            self._journey(k) for k in ("multi_page", "single_engaged", "single_passive")
        )
        self.assertEqual(total, confirmed)

    def test_a_list_click_counts_as_using_the_page(self):
        """Browsing by list card used to be invisible and filed as 'read one page and left'."""
        self._event(self.today, visitor="lister", event="pageview_js")
        self._event(self.today, visitor="lister", event="card_click", key="p")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(self._journey("single_engaged"), 1)
        self.assertEqual(self._journey("single_passive"), 0)

    def test_list_clicks_are_broken_down_per_pool(self):
        self._event(self.today, visitor="aaa", event="card_click", key="mander")
        self._event(self.today, visitor="bbb", event="card_click", key="mander")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(
            UsageDaily.objects.get(day=self.today, metric="card_click", key="mander").visitors, 2
        )

    def test_unconfirmed_visitors_are_left_out_of_the_split(self):
        """A no-JS client can't be told apart from a reader who did nothing."""
        self._event(self.today, visitor="quiet")            # HTML only, never confirmed
        self._event(self.today, visitor="bot1", client_class="bot", event="pageview_js")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(self._journey("single_passive"), 0)

    def test_reloading_one_page_is_not_two_pages(self):
        self._event(self.today, visitor="aaa", event="pageview_js")
        self._event(self.today, visitor="aaa")
        self._event(self.today, visitor="aaa")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(self._journey("single_passive"), 1)
        self.assertEqual(self._journey("multi_page"), 0)

    def test_two_different_pool_pages_count_as_two_pages(self):
        self._event(self.today, visitor="aaa", event="pool_view", key="one")
        self._event(self.today, visitor="aaa", event="pool_view", key="two")
        self._event(self.today, visitor="aaa", event="pageview_js")
        call_command("rollup_usage", verbosity=0)
        self.assertEqual(self._journey("multi_page"), 1)

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
        UsageEvent.objects.create(
            day=timezone.localdate(), event="pool_view", key=pool.slug, visitor="aaa"
        )
        call_command("rollup_usage", verbosity=0)
        resp = self.client.get("/stats/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Test Pool")

    def test_interaction_tile_reports_confirmed_browsers_only(self):
        from django.contrib.auth.models import User
        User.objects.create_superuser("admin4", "a4@example.com", "pw")
        self.client.login(username="admin4", password="pw")
        day = timezone.localdate()
        UsageEvent.objects.create(day=day, event="index", visitor="aaa")
        UsageEvent.objects.create(day=day, event="pageview_js", visitor="aaa")
        UsageEvent.objects.create(day=day, event="filter", visitor="aaa")
        UsageEvent.objects.create(day=day, event="index", visitor="nojs")
        call_command("rollup_usage", verbosity=0)

        resp = self.client.get("/stats/?days=7")
        self.assertEqual(resp.context["totals"]["confirmed_events"], 2)
        self.assertEqual(resp.context["totals"]["events"], 4)   # every human row

    def test_interaction_types_chart_leaves_out_the_beacon(self):
        from django.contrib.auth.models import User
        User.objects.create_superuser("admin5", "a5@example.com", "pw")
        self.client.login(username="admin5", password="pw")
        day = timezone.localdate()
        for n in range(5):
            UsageEvent.objects.create(day=day, event="pageview_js", visitor=f"v{n}")
            UsageEvent.objects.create(day=day, event="index", visitor=f"v{n}")
        call_command("rollup_usage", verbosity=0)

        resp = self.client.get("/stats/?days=7")
        keys = [r["key"] for r in resp.context["events_by_type"]]
        self.assertNotIn("pageview_js", keys)
        self.assertIn("index", keys)
        # Dropped from the chart only — the stored count is still there.
        self.assertTrue(
            UsageDaily.objects.filter(day=day, metric="event", key="pageview_js").exists()
        )

    def test_collection_start_shows_only_when_the_window_reaches_it(self):
        from django.contrib.auth.models import User
        User.objects.create_superuser("admin3", "a3@example.com", "pw")
        self.client.login(username="admin3", password="pw")
        began = timezone.localdate() - timedelta(days=5)
        UsageEvent.objects.create(day=began, event="index", visitor="aaa")
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
            day=timezone.localdate() - timedelta(days=3), event="index", visitor="old"
        )
        UsageEvent.objects.create(day=timezone.localdate(), event="index", visitor="new")
        call_command("rollup_usage", verbosity=0)

        resp = self.client.get("/stats/?days=1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["series"]), 1)
        self.assertEqual(resp.context["totals"]["visitors"], 1)
        self.assertEqual(resp.context["first_day"], timezone.localdate())
