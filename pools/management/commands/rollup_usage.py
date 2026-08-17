"""
Aggregate raw UsageEvent rows into permanent UsageDaily counts, then prune the raw
rows once they age out.

Safe to run repeatedly — days are recomputed in place, so running this on every
cron pass keeps the /stats/ page near-live without needing its own schedule.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import ExtractHour
from django.utils import timezone

from pools.models import UsageDaily, UsageEvent, UsageRollupState
from pools.services.usage import (
    AUDIENCE_BOT,
    AUDIENCE_CONFIRMED,
    AUDIENCE_HUMAN,
    BOT_UA_FAMILIES,
    JOURNEY_MULTI_PAGE,
    JOURNEY_SINGLE_ENGAGED,
    JOURNEY_SINGLE_PASSIVE_DETAIL,
    JOURNEY_SINGLE_PASSIVE_LIST,
    JOURNEY_SINGLE_PASSIVE_OTHER,
    JS_ONLY_EVENTS,
    USAGE_RAW_RETENTION_DAYS,
    browser_family,
    classify_journey,
    is_stale_version,
)

# (metric name, UsageEvent field) breakdowns rolled up verbatim, skipping blanks.
_FIELD_METRICS = [
    ("status_filter", "status_filter"),
    ("neighborhood", "neighborhood"),
    ("zip", "zip_searched"),
    ("referrer", "referrer_host"),
    ("device", "device"),
    ("browser", "ua_family"),
]


class Command(BaseCommand):
    help = "Roll raw usage events up into daily counts and prune expired raw rows."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, default=3,
            help="How many recent days to recompute (default 3, enough to cover a "
                 "missed cron run without rewriting the whole season).",
        )
        parser.add_argument(
            "--all", action="store_true",
            help="Recompute every day that still has raw rows.",
        )
        parser.add_argument(
            "--no-prune", action="store_true",
            help="Aggregate but keep raw rows (useful before re-tuning bot classification).",
        )

    def handle(self, *args, **options):
        if options["all"]:
            days = set(UsageEvent.objects.values_list("day", flat=True).distinct())
        else:
            today = timezone.localdate()
            days = {today - timedelta(days=n) for n in range(options["days"])}
            # Also sweep up any day that has raw rows but was never aggregated. Without
            # this, a cron outage longer than --days would leave those days unrolled
            # while the prune below eventually deleted them: silent data loss with no
            # error anywhere. Cheap to check, and it makes an outage self-healing.
            rolled_up = set(UsageDaily.objects.values_list("day", flat=True).distinct())
            unrolled = set(UsageEvent.objects.values_list("day", flat=True).distinct())
            days |= unrolled - rolled_up

        days = sorted(days)

        for day in days:
            counts = self._rollup_day(day)
            self.stdout.write(f"{day}: {counts} metric rows")

        if not options["no_prune"]:
            cutoff = timezone.localdate() - timedelta(days=USAGE_RAW_RETENTION_DAYS)
            # Only drop raw rows for days whose counts are safely in UsageDaily, so a
            # day can never be deleted before it has been aggregated.
            aggregated = list(UsageDaily.objects.values_list("day", flat=True).distinct())
            deleted, _ = UsageEvent.objects.filter(
                day__lt=cutoff, day__in=aggregated
            ).delete()
            self.stdout.write(f"Pruned {deleted} raw rows older than {cutoff}")

        # Recorded even when there was nothing to aggregate: a quiet pass still
        # means the figures on /stats/ are current as of now.
        state = UsageRollupState.load()
        state.last_run_at = timezone.now()
        state.save(update_fields=["last_run_at"])

    @transaction.atomic
    def _rollup_day(self, day):
        events = UsageEvent.objects.filter(day=day)
        if not events.exists():
            return 0

        # A bot verdict belongs to the visitor, not to the one request that earned it.
        # Three things make that necessary: the header forgery check only runs on page
        # navigations, so a spoofed browser's own beacon POST would come back
        # "unknown" and quietly readmit it; a scanner's probe has to discredit the
        # ordinary-looking requests it made alongside; and someone walking the
        # retired numeric pool IDs with no referrer looks, on the redirected request
        # itself, like an ordinary pool_view. One bot row taints the day.
        #
        # BOT_UA_FAMILIES is tested here as well as in the classifier, and not only
        # for symmetry: rows written before that rule shipped carry client_class
        # "unknown", and the family is the one thing about them that survived. Asking
        # the question of the stored column is what makes the rule retroactive over
        # every day whose raw rows are still around.
        caught = set(
            events.filter(
                Q(client_class="bot")
                | Q(event="probe")
                | Q(event="legacy_id")
                | Q(ua_family__in=BOT_UA_FAMILIES)
            )
            .values_list("visitor", flat=True)
            .distinct()
        )
        rest = events.exclude(visitor__in=caught)

        # A visitor is a confirmed browser once they hit an endpoint only the page's
        # own JavaScript requests. Everyone else is merely not-obviously-a-bot.
        confirmed = set(
            rest.filter(event__in=JS_ONLY_EVENTS).values_list("visitor", flat=True).distinct()
        )

        # Rows written before the user-agent checks shipped carry no ua_family; every
        # row since carries at least "other". So a visitor with no family on any row is
        # one the old, weaker rules let through, and one the new ones can never be run
        # against — the headers they would have been judged on were never stored.
        # Sampling the live logs when those rules were written put roughly four in five
        # of that leftover traffic squarely in the scanner camp, so it is filed as bot
        # rather than left inflating the visitor count. The minority that were people
        # browsing with JavaScript off are lost to it; that is the price of not knowing.
        # Self-limiting: raw rows expire, and once a day's have gone it is never
        # recomputed, so this can only ever touch the handful of days that predate the
        # column and still have raw rows to be rebuilt from.
        staff_visitors = set(
            rest.filter(client_class="staff").values_list("visitor", flat=True).distinct()
        )
        legacy = (
            set(rest.values_list("visitor", flat=True).distinct())
            - set(rest.exclude(ua_family="").values_list("visitor", flat=True).distinct())
            - confirmed
            - staff_visitors
        )

        # Unconfirmed traffic arriving from a hosting provider's address range. The
        # gate on `confirmed` is the whole point of the rule rather than a caveat to
        # it: people really do browse from behind VPNs and relays that live in those
        # same ranges, and anyone whose browser ran the page has already proved they
        # are not the thing this is looking for. What is left is a machine that
        # fetched HTML from a rack and did nothing a browser does.
        from_racks = set(
            rest.filter(datacenter=True).values_list("visitor", flat=True).distinct()
        )
        datacenter = from_racks - confirmed - staff_visitors

        # Traffic claiming a browser version too old to still be in the wild, which
        # also never ran the page's JavaScript. Gated on `confirmed` for the same
        # reason the rack rule is, and for one more: a frozen version is strong
        # evidence about a fleet but weak evidence about any single visitor, since a
        # laptop shut in a drawer since spring really does wake up on an old Chrome.
        # Having run the page settles it either way, so nobody who did is touched.
        #
        # Read off the stored family rather than the string, so the rule reaches back
        # over every day whose raw rows survive — the same property that makes
        # BOT_UA_FAMILIES retroactive.
        stale_families = [
            family
            for family in rest.exclude(ua_family="").values_list("ua_family", flat=True).distinct()
            if is_stale_version(family)
        ]
        stale = set(
            rest.filter(ua_family__in=stale_families).values_list("visitor", flat=True).distinct()
        ) - confirmed - staff_visitors

        bot_visitors = caught | legacy | datacenter | stale
        # Bots are counted, not discarded — their crawl pattern is the only view we
        # have of what Googlebot actually fetches — but they are kept out of every
        # human-facing metric below.
        bots = events.filter(visitor__in=bot_visitors)
        human = events.exclude(visitor__in=bot_visitors)
        rows = {}

        def put(metric, key, events_count, visitors_count, audience=AUDIENCE_HUMAN):
            rows[(metric, key or "", audience)] = (events_count, visitors_count)

        put("visitors", "", human.count(), human.values("visitor").distinct().count())
        put("visitors", "bot", bots.count(), len(bot_visitors))
        # Staff are deliberately inside the "visitors" total above, not subtracted
        # from it — this is the share of it that was us.
        put("visitors", "staff",
            human.filter(client_class="staff").count(),
            human.filter(client_class="staff").values("visitor").distinct().count())

        # `events` here is what confirmed browsers did, minus the page-load beacon:
        # it fires by itself on every page and duplicates the page view it
        # accompanies, so counting it would roughly double the figure. Unlike the
        # plain "visitors" row above, this one is deliberately not a raw event count.
        put("visitors", "js_confirmed",
            human.filter(visitor__in=confirmed).exclude(event="pageview_js").count(),
            len(confirmed), audience=AUDIENCE_CONFIRMED)

        # How far each confirmed browser got: more than one page, one page but they
        # used the map or filters, or one page and nothing else (split by whether
        # that one page was the pool list, a pool detail page, or something else).
        # Only confirmed browsers are split — for anyone else "did nothing" can't be
        # told apart from "ran no JavaScript", which would file every bot as a bored
        # reader. These buckets partition `confirmed`, so they add back up to it.
        by_visitor = {}
        for visitor, event, key in human.filter(visitor__in=confirmed).values_list(
            "visitor", "event", "key"
        ):
            by_visitor.setdefault(visitor, []).append((event, key))

        journeys = {
            JOURNEY_MULTI_PAGE: [],
            JOURNEY_SINGLE_ENGAGED: [],
            JOURNEY_SINGLE_PASSIVE_LIST: [],
            JOURNEY_SINGLE_PASSIVE_DETAIL: [],
            JOURNEY_SINGLE_PASSIVE_OTHER: [],
        }
        for visitor, events in by_visitor.items():
            journeys[classify_journey(events)].append(visitor)

        for journey, visitors in journeys.items():
            put("journey", journey,
                sum(len(by_visitor[v]) for v in visitors), len(visitors),
                audience=AUDIENCE_CONFIRMED)

        # Every ranked breakdown is stored twice: once for all non-robot visitors and
        # once for confirmed browsers alone. /stats/ defaults to the confirmed view and
        # offers the wider one, and storing both now is the only way to keep that choice
        # available after the raw rows have been pruned.
        for audience, audience_events in (
            (AUDIENCE_HUMAN, human),
            (AUDIENCE_CONFIRMED, human.filter(visitor__in=confirmed)),
        ):
            for row in audience_events.values("event").annotate(
                events=Count("id"), visitors=Count("visitor", distinct=True)
            ):
                put("event", row["event"], row["events"], row["visitors"], audience)

            for event_name, metric in [
                ("pool_view", "pool_view"), ("pin_click", "pin_click"),
                ("card_click", "card_click"), ("nearby_click", "nearby_click"),
            ]:
                for row in audience_events.filter(event=event_name).exclude(key="").values(
                    "key"
                ).annotate(events=Count("id"), visitors=Count("visitor", distinct=True)):
                    put(metric, row["key"], row["events"], row["visitors"], audience)

            for metric, field in _FIELD_METRICS:
                for row in audience_events.exclude(**{field: ""}).values(field).annotate(
                    events=Count("id"), visitors=Count("visitor", distinct=True)
                ):
                    put(metric, row[field], row["events"], row["visitors"], audience)

            # The same browsers with the version dropped, so the ranked table can be
            # read as "which browser", which the versioned one cannot: see
            # browser_family(). Aggregated here rather than summed on the page
            # because the visitor figures are distinct counts — somebody who was on
            # chrome/151 in the morning and chrome/152 after an update is one Chrome
            # user, and adding the two rows would make them two.
            family_events = {}
            family_visitors = {}
            for row in (
                audience_events.exclude(ua_family="")
                .values("ua_family", "visitor")
                .annotate(events=Count("id"))
            ):
                family = browser_family(row["ua_family"])
                family_events[family] = family_events.get(family, 0) + row["events"]
                family_visitors.setdefault(family, set()).add(row["visitor"])

            for family, event_count in family_events.items():
                put("browser_family", family, event_count,
                    len(family_visitors[family]), audience)

            # What time of day it was. `created_at` is the only sub-day detail a raw
            # row carries and the only field no stored metric preserves, so without
            # this the question "when do people check" becomes unanswerable the moment
            # the rows are pruned — and unanswerable retroactively, forever.
            #
            # Local hours, not UTC: the question is what time it was in Philadelphia,
            # where the answer plausibly differs between deciding to go this morning
            # and planning tomorrow from the couch. USE_TZ is on, so ExtractHour
            # converts into TIME_ZONE before extracting.
            #
            # Zero-padded so the keys sort as hours rather than as "0, 1, 10, 11, 2".
            # These rows deliberately do not sum to the day: someone who looks at 9am
            # and again at 6pm is a visitor in both hours, which is what makes the
            # count mean "how many people were here then" instead of a share of a
            # total. `events` includes the page-load beacon, unlike the js_confirmed
            # row above — it fires once per page for everyone, so it lifts every hour
            # alike and leaves the shape across the day, which is the point here,
            # untouched.
            for row in (
                audience_events.annotate(hour=ExtractHour("created_at"))
                .values("hour")
                .annotate(events=Count("id"), visitors=Count("visitor", distinct=True))
            ):
                put("hour", f"{row['hour']:02d}", row["events"], row["visitors"], audience)

        # The datacenter check's working, not just its verdict, keyed by the browser
        # family the traffic claimed to be.
        #
        # This is what makes a poor confirmation rate diagnosable. A family that
        # rarely runs the page's JavaScript is either real people whose beacon is
        # being blocked or lost, or crawlers wearing that family's name — opposite
        # conclusions, and whether the traffic came from a hosting provider's rack is
        # the only thing that separates them. The flag lives on raw rows alone, so
        # the question becomes unanswerable the moment they are pruned.
        #
        #   bot       — from a rack and never ran the page: what the rule caught
        #   confirmed — from a rack but ran the page anyway, so a person behind a VPN
        #               or relay, which is exactly who the rule is written to spare
        #
        # There is no "human" row by construction: an unconfirmed visitor from a rack
        # is already in the bot set, so those two rows are the whole population.
        # Blank families are skipped like every other breakdown — they only exist on
        # rows predating the column, which cannot be judged either way.
        for audience, group in (
            (AUDIENCE_BOT, datacenter),
            (AUDIENCE_CONFIRMED, from_racks & confirmed),
        ):
            for row in (
                rest.filter(visitor__in=group).exclude(ua_family="")
                .values("ua_family")
                .annotate(events=Count("id"), visitors=Count("visitor", distinct=True))
            ):
                put("datacenter", row["ua_family"], row["events"], row["visitors"], audience)

        # The stale-version rule's working, stored the same way and for the same
        # reason: a family it empties out would otherwise vanish from /stats/ with no
        # trace of why, leaving the rule unauditable the moment the raw rows expire.
        # Bot audience only — a stale version that ran the JavaScript was spared, and
        # is already counted as the ordinary visitor the rule decided it was.
        for row in (
            rest.filter(visitor__in=stale).exclude(ua_family="")
            .values("ua_family")
            .annotate(events=Count("id"), visitors=Count("visitor", distinct=True))
        ):
            put("stale_version", row["ua_family"], row["events"], row["visitors"], AUDIENCE_BOT)

        UsageDaily.objects.filter(day=day).delete()
        UsageDaily.objects.bulk_create([
            UsageDaily(day=day, metric=metric, key=key, audience=audience, events=e, visitors=v)
            for (metric, key, audience), (e, v) in rows.items()
        ])
        return len(rows)
