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
from django.utils import timezone

from pools.models import UsageDaily, UsageEvent, UsageRollupState
from pools.services.usage import (
    AUDIENCE_CONFIRMED,
    AUDIENCE_HUMAN,
    JOURNEY_MULTI_PAGE,
    JOURNEY_SINGLE_ENGAGED,
    JOURNEY_SINGLE_PASSIVE_DETAIL,
    JOURNEY_SINGLE_PASSIVE_LIST,
    JOURNEY_SINGLE_PASSIVE_OTHER,
    JS_ONLY_EVENTS,
    USAGE_RAW_RETENTION_DAYS,
    classify_journey,
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
        # Two things make that necessary: the header forgery check only runs on page
        # navigations, so a spoofed browser's own beacon POST would come back
        # "unknown" and quietly readmit it; and a scanner's probe has to discredit the
        # ordinary-looking requests it made alongside. One bot row taints the day.
        caught = set(
            events.filter(Q(client_class="bot") | Q(event="probe"))
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
        datacenter = (
            set(rest.filter(datacenter=True).values_list("visitor", flat=True).distinct())
            - confirmed
            - staff_visitors
        )

        bot_visitors = caught | legacy | datacenter
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
                ("pool_view", "pool_view"), ("pin_click", "pin_click"), ("card_click", "card_click"),
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

        UsageDaily.objects.filter(day=day).delete()
        UsageDaily.objects.bulk_create([
            UsageDaily(day=day, metric=metric, key=key, audience=audience, events=e, visitors=v)
            for (metric, key, audience), (e, v) in rows.items()
        ])
        return len(rows)
