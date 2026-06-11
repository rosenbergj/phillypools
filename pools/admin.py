from datetime import date

from django.contrib import admin, messages
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path
from django.utils import timezone
from django.utils.html import format_html

from pools.models import Pool, ScheduleChange, Submission


@admin.register(Pool)
class PoolAdmin(admin.ModelAdmin):
    list_display = ["name", "pool_type", "neighborhood", "is_open_display", "opening_date", "closing_date", "is_active"]
    list_filter = ["pool_type", "is_active", "neighborhood"]
    search_fields = ["name", "address", "neighborhood"]
    readonly_fields = ["last_updated"]

    def is_open_display(self, obj):
        v = obj.is_open
        if v is True:
            return "Open"
        if v is False:
            return "Closed"
        return "?"
    is_open_display.short_description = "Status"


class ScheduleChangeInline(admin.TabularInline):
    model = ScheduleChange
    extra = 0


PoolAdmin.inlines = [ScheduleChangeInline]


def _source_url(submission):
    """Return the best URL to use as a source attribution for this submission."""
    return submission.url or ""


def apply_to_pool(modeladmin, request, queryset):
    applied = skipped = 0
    for sub in queryset.select_related("parsed_pool"):
        if not sub.parsed_pool:
            skipped += 1
            continue

        pool = sub.parsed_pool
        source = _source_url(sub)

        if sub.parsed_opening_date:
            pool.opening_date = sub.parsed_opening_date
            pool.opening_date_source_url = source
        if sub.parsed_closing_date:
            pool.closing_date = sub.parsed_closing_date
            pool.closing_date_source_url = source
        if sub.parsed_hours:
            pool.hours = sub.parsed_hours
            pool.hours_source_url = source
        if sub.parsed_weekday_schedule:
            pool.weekday_schedule = sub.parsed_weekday_schedule
            pool.weekday_schedule_source_url = source
        if sub.parsed_weekend_schedule:
            pool.weekend_schedule = sub.parsed_weekend_schedule
            pool.weekend_schedule_source_url = source
        if sub.parsed_notes:
            pool.updates = sub.parsed_notes
            pool.updates_source_url = source

        pool.save()

        sub.status = "approved"
        sub.reviewed_at = timezone.now()
        sub.save()
        applied += 1

    msg = f"Applied {applied} submission(s) to pool(s)."
    if skipped:
        msg += f" Skipped {skipped} with no linked pool."
    modeladmin.message_user(request, msg)

apply_to_pool.short_description = "Apply parsed data to linked pool"


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    change_form_template = "admin/pools/submission_change_form.html"
    list_display = ["short_source", "submitted_at", "parsed_pool", "llm_confidence", "status"]
    list_filter = ["status", "llm_confidence"]
    list_select_related = ["parsed_pool"]
    actions = [apply_to_pool]
    readonly_fields = [
        "submitted_at",
        "image_preview",
        "llm_response_display",
        "current_opening_date",
        "current_closing_date",
        "current_hours",
        "current_weekday_schedule",
        "current_weekend_schedule",
        "current_updates",
    ]
    fieldsets = (
        ("Submission", {
            "fields": (
                "submitted_at",
                "url",
                "uploaded_image",
                "image_preview",
                "submitter_note",
            ),
        }),
        ("Parsed fields vs. current pool values", {
            "description": "Left column: what the LLM extracted. Right column: what the pool currently has. Parsed notes → Updates field.",
            "fields": (
                ("parsed_pool", "llm_confidence"),
                ("parsed_opening_date", "current_opening_date"),
                ("parsed_closing_date", "current_closing_date"),
                ("parsed_hours", "current_hours"),
                ("parsed_weekday_schedule", "current_weekday_schedule"),
                ("parsed_weekend_schedule", "current_weekend_schedule"),
                ("parsed_notes", "current_updates"),
            ),
        }),
        ("Review", {
            "fields": ("status", "reviewed_at", "moderator_notes"),
        }),
        ("Raw LLM response", {
            "classes": ("collapse",),
            "fields": ("llm_response_display",),
        }),
    )

    def get_urls(self):
        return [
            path("<int:submission_id>/reparse/",
                 self.admin_site.admin_view(self.reparse_view),
                 name="pools_submission_reparse"),
            path("<int:submission_id>/apply/",
                 self.admin_site.admin_view(self.apply_view),
                 name="pools_submission_apply"),
        ] + super().get_urls()

    def apply_view(self, request, submission_id):
        apply_to_pool(self, request, Submission.objects.filter(pk=submission_id))
        return redirect(f"../../{submission_id}/change/")

    def reparse_view(self, request, submission_id):
        submission = get_object_or_404(Submission, pk=submission_id)

        if request.method == "POST" and request.POST.get("action") == "apply":
            # Apply selected pool updates
            source = submission.url or ""
            applied = 0
            for pool_id in request.POST.getlist("pool_ids"):
                try:
                    pool = Pool.objects.get(pk=pool_id)
                    opening = request.POST.get(f"opening_date_{pool_id}")
                    closing = request.POST.get(f"closing_date_{pool_id}")
                    hours = request.POST.get(f"hours_{pool_id}", "")
                    notes = request.POST.get(f"notes_{pool_id}", "")
                    if opening:
                        pool.opening_date = date.fromisoformat(opening)
                        pool.opening_date_source_url = source
                    if closing:
                        pool.closing_date = date.fromisoformat(closing)
                        pool.closing_date_source_url = source
                    if hours:
                        pool.hours = hours
                        pool.hours_source_url = source
                    if notes:
                        pool.updates = notes
                        pool.updates_source_url = source
                    pool.save()
                    applied += 1
                except (Pool.DoesNotExist, ValueError):
                    pass
            if applied:
                submission.status = "approved"
                submission.reviewed_at = timezone.now()
                submission.save()
            messages.success(request, f"Applied updates to {applied} pool(s).")
            return redirect(f"../../{submission_id}/change/")

        # GET or POST without action=apply: run the LLM
        results = []
        error = None
        pool_list = list(Pool.objects.filter(is_active=True).values("id", "name"))
        try:
            if submission.url:
                from pools.services.url_fetcher import fetch_url
                from pools.services.llm_parser import parse_all_pools
                results = parse_all_pools(fetch_url(submission.url), pool_list)
            elif submission.uploaded_image:
                from pools.services.llm_parser import parse_all_pools_image
                results = parse_all_pools_image(submission.uploaded_image.read(), submission.uploaded_image.name, pool_list)
            else:
                error = "This submission has no URL or image to parse."
        except Exception as e:
            error = str(e)

        # Attach matched Pool objects
        pool_by_id = {p.id: p for p in Pool.objects.filter(is_active=True)}
        for r in results:
            r["pool_obj"] = pool_by_id.get(r.get("pool_id"))

        return TemplateResponse(request, "admin/pools/submission_reparse.html", {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "submission": submission,
            "results": results,
            "error": error,
            "title": "Re-parse for all pools",
        })

    def short_source(self, obj):
        if obj.url:
            return obj.url[:60] + ("…" if len(obj.url) > 60 else "")
        if obj.uploaded_image:
            return f"[image] {obj.uploaded_image.name.split('/')[-1]}"
        return "—"
    short_source.short_description = "Source"

    def image_preview(self, obj):
        if obj.uploaded_image:
            return format_html(
                '<img src="{}" style="max-width:400px;max-height:300px;object-fit:contain">',
                obj.uploaded_image.url,
            )
        return "—"
    image_preview.short_description = "Image preview"

    def llm_response_display(self, obj):
        if obj.llm_response:
            import json
            return format_html("<pre style='white-space:pre-wrap'>{}</pre>",
                               json.dumps(obj.llm_response, indent=2))
        return "—"
    llm_response_display.short_description = "Raw LLM response"

    def _current(self, obj, field):
        if obj.parsed_pool:
            return getattr(obj.parsed_pool, field) or "—"
        return "(no pool linked)"

    def current_opening_date(self, obj):
        return self._current(obj, "opening_date")
    current_opening_date.short_description = "Current: opening date"

    def current_closing_date(self, obj):
        return self._current(obj, "closing_date")
    current_closing_date.short_description = "Current: closing date"

    def current_hours(self, obj):
        return self._current(obj, "hours")
    current_hours.short_description = "Current: hours"

    def current_weekday_schedule(self, obj):
        return self._current(obj, "weekday_schedule")
    current_weekday_schedule.short_description = "Current: weekday schedule"

    def current_weekend_schedule(self, obj):
        return self._current(obj, "weekend_schedule")
    current_weekend_schedule.short_description = "Current: weekend schedule"

    def current_updates(self, obj):
        return self._current(obj, "updates")
    current_updates.short_description = "Current: updates"
