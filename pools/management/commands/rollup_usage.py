"""
Aggregate raw UsageEvent rows into permanent UsageDaily counts, then prune the raw
rows once they age out.

Safe to run repeatedly — days are recomputed in place, so running this on every
cron pass keeps the /stats/ page near-live without needing its own schedule.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from pools.models import UsageDaily, UsageEvent, UsageRollupState
from pools.services.usage import JS_ONLY_EVENTS, USAGE_RAW_RETENTION_DAYS

# (metric name, UsageEvent field) breakdowns rolled up verbatim, skipping blanks.
_FIELD_METRICS = [
    ("status_filter", "status_filter"),
    ("neighborhood", "neighborhood"),
    ("zip", "zip_searched"),
    ("referrer", "referrer_host"),
    ("device", "device"),
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

        # Bots are counted, not discarded — their crawl pattern is the only view we
        # have of what Googlebot actually fetches — but they are kept out of every
        # human-facing metric below.
        human = events.exclude(client_class="bot")
        rows = {}

        def put(metric, key, events_count, visitors_count):
            rows[(metric, key or "")] = (events_count, visitors_count)

        put("visitors", "", human.count(), human.values("visitor").distinct().count())
        put("visitors", "bot",
            events.filter(client_class="bot").count(),
            events.filter(client_class="bot").values("visitor").distinct().count())
        # Staff are deliberately inside the "visitors" total above, not subtracted
        # from it — this is the share of it that was us.
        put("visitors", "staff",
            human.filter(client_class="staff").count(),
            human.filter(client_class="staff").values("visitor").distinct().count())

        # A visitor is a confirmed browser once they hit an endpoint only the page's
        # own JavaScript requests. Everyone else is merely not-obviously-a-bot.
        confirmed = set(
            human.filter(event__in=JS_ONLY_EVENTS).values_list("visitor", flat=True).distinct()
        )
        put("visitors", "js_confirmed",
            human.filter(visitor__in=confirmed).count(), len(confirmed))

        for row in human.values("event").annotate(
            events=Count("id"), visitors=Count("visitor", distinct=True)
        ):
            put("event", row["event"], row["events"], row["visitors"])

        for event_name, metric in [("pool_view", "pool_view"), ("pin_click", "pin_click")]:
            for row in human.filter(event=event_name).exclude(key="").values("key").annotate(
                events=Count("id"), visitors=Count("visitor", distinct=True)
            ):
                put(metric, row["key"], row["events"], row["visitors"])

        for metric, field in _FIELD_METRICS:
            for row in human.exclude(**{field: ""}).values(field).annotate(
                events=Count("id"), visitors=Count("visitor", distinct=True)
            ):
                put(metric, row[field], row["events"], row["visitors"])

        UsageDaily.objects.filter(day=day).delete()
        UsageDaily.objects.bulk_create([
            UsageDaily(day=day, metric=metric, key=key, events=e, visitors=v)
            for (metric, key), (e, v) in rows.items()
        ])
        return len(rows)
