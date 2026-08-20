from datetime import date

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import ShowFacets
from django.contrib.admin.templatetags.admin_list import _boolean_icon
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import path
from django.utils import timezone
from django.utils.html import escape, format_html, format_html_join, mark_safe

from django.db.models import Q

from pools.models import DigestState, HeatEmergencyPressRelease, HeatHealthEmergency, MonitoredPage, Pool, PoolAlternateName, PoolGisState, PoolLike, PoolSeasonHistory, ScheduleChange, SiteAnnouncement, Submission


class SubmissionImageWidget(forms.Widget):
    """Radio-button image picker for selecting a submission's image on a pool page."""

    def __init__(self, queryset=None, current_year=None, show_all=False, toggle_url=None, attrs=None):
        super().__init__(attrs)
        self._queryset = list(queryset) if queryset is not None else []
        self._current_year = current_year
        self._show_all = show_all
        self._toggle_url = toggle_url

    def render(self, name, value, attrs=None, renderer=None):
        try:
            current_pk = int(value) if value else None
        except (ValueError, TypeError):
            current_pk = None

        submissions = [s for s in self._queryset if s.uploaded_image]

        if not submissions:
            if self._toggle_url:
                return mark_safe(
                    f'<p style="color:#666;margin:0">No approved images from this year for this pool. '
                    f'<a href="{escape(self._toggle_url)}">Show all submissions</a></p>'
                )
            return mark_safe('<p style="color:#666;margin:0">No images have been submitted for this pool.</p>')

        html = ['<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:flex-start;padding:8px 0">']

        none_checked = ' checked' if current_pk is None else ''
        html.append(
            f'<label style="display:flex;flex-direction:column;align-items:center;gap:6px;cursor:pointer">'
            f'<input type="radio" name="{name}" value=""{none_checked}>'
            f'<span style="font-size:.85em;color:#666">None</span>'
            f'</label>'
        )

        for sub in submissions:
            selected = ' checked' if current_pk == sub.pk else ''
            is_current_year = sub.submitted_at.year == self._current_year
            is_approved = sub.status == 'approved'

            date_str = sub.submitted_at.strftime('%b %-d, %Y')
            date_html = f'<strong>{date_str}</strong>' if is_current_year else date_str

            if is_approved:
                container_style = 'display:flex;flex-direction:column;align-items:center;gap:4px;cursor:pointer'
                status_html = ''
            else:
                container_style = (
                    'display:flex;flex-direction:column;align-items:center;gap:4px;cursor:pointer;'
                    'background:#fffbe6;border:1px solid #ffe58f;border-radius:4px;padding:6px'
                )
                status_html = (
                    f'<span style="font-size:.75em;padding:1px 5px;background:#faad14;'
                    f'border-radius:3px;display:block;text-align:center">{escape(sub.status)}</span>'
                )

            img_url = escape(sub.uploaded_image.url)
            html.append(
                f'<label style="{container_style}">'
                f'<input type="radio" name="{name}" value="{sub.pk}"{selected}>'
                f'<img src="{img_url}" style="max-width:150px;max-height:120px;object-fit:contain">'
                f'<span style="font-size:.85em;text-align:center">{date_html}</span>'
                f'{status_html}'
                f'</label>'
            )

        html.append('</div>')

        if self._toggle_url:
            toggle = escape(self._toggle_url)
            html.append(
                f'<p style="margin-top:4px;font-size:.9em">'
                f'<a href="{toggle}">Also show older and non-approved images</a></p>'
            )

        return mark_safe(''.join(html))

    def value_from_datadict(self, data, files, name):
        val = data.get(name)
        return val if val else None


class PoolStatusFilter(admin.SimpleListFilter):
    title = "status"
    parameter_name = "status"

    def lookups(self, request, model_admin):
        return [("open", "Open"), ("closed", "Closed")]

    def queryset(self, request, queryset):
        today = date.today()
        open_qs = queryset.filter(
            is_active=True,
            opening_date__lte=today,
        ).filter(Q(closing_date__isnull=True) | Q(closing_date__gte=today))
        if self.value() == "open":
            return open_qs
        if self.value() == "closed":
            return queryset.exclude(pk__in=open_qs)
        return queryset


@admin.register(Pool)
class PoolAdmin(admin.ModelAdmin):
    list_display = ["name", "neighborhood", "social_media_display", "is_open_display", "opening_date_display", "closing_date_display", "schedule_display", "is_active"]
    list_filter = [PoolStatusFilter, "is_active", "ada_lift", "neighborhood"]
    list_editable = ["is_active"]
    search_fields = ["name", "address", "neighborhood"]
    readonly_fields = ["last_updated", "display_image_preview"]
    fieldsets = [
        (None, {"fields": (
            "name", "slug", "ppr_amenity_id", "address", "neighborhood",
            "latitude", "longitude", "pool_type", "is_active",
            "phillypublicpools_url", "social_media_url", "phone_number",
        )}),
        ("Season", {"fields": (
            "opening_date", "opening_date_source_url",
            "closing_date", "closing_date_source_url",
        )}),
        ("Schedule", {"fields": (
            "weekday_schedule", "weekday_schedule_source_url",
            "weekend_schedule", "weekend_schedule_source_url",
        )}),
        ("Info", {"fields": (
            "ada_lift",
            "notes",
            "updates", "updates_source_url",
        )}),
        ("DISPLAY IMAGE", {"fields": (
            "display_image_preview", "display_image_submission", "display_image_caption",
        )}),
        ("Metadata", {"fields": ("last_updated",), "classes": ("collapse",)}),
    ]

    def display_image_preview(self, obj):
        sub = obj.display_image_submission
        if sub and sub.uploaded_image:
            return format_html(
                '<img src="{}" style="max-width:400px;max-height:300px;object-fit:contain">',
                sub.uploaded_image.url,
            )
        return "—"
    display_image_preview.short_description = "Current image"

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == "display_image_submission" and formfield:
            w = formfield.widget
            if hasattr(w, 'can_add_related'):
                w.can_add_related = False
                w.can_change_related = False
                w.can_delete_related = False
                w.can_view_related = False
        return formfield

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "display_image_submission":
            pool_pk = request.resolver_match.kwargs.get('object_id')
            year = timezone.now().year
            show_all = bool(request.GET.get('show_all_images'))
            if pool_pk:
                full_qs = (
                    Submission.objects
                    .filter(parsed_pool_id=pool_pk)
                    .exclude(uploaded_image='')
                    .order_by('-submitted_at')
                )
                if show_all:
                    display_qs = full_qs
                    toggle_url = None
                else:
                    display_qs = full_qs.filter(status='approved', submitted_at__year=year)
                    has_extra = full_qs.exclude(status='approved', submitted_at__year=year).exists()
                    toggle_url = (request.path + '?show_all_images=1') if has_extra else None
            else:
                full_qs = Submission.objects.none()
                display_qs = Submission.objects.none()
                toggle_url = None
            kwargs["queryset"] = full_qs
            kwargs["widget"] = SubmissionImageWidget(
                queryset=display_qs,
                current_year=year,
                show_all=show_all,
                toggle_url=toggle_url,
            )
            kwargs["required"] = False
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def is_open_display(self, obj):
        if obj.is_open is True:
            return "Open"
        return "Closed"
    is_open_display.short_description = "Status"

    def opening_date_display(self, obj):
        return obj.opening_date.strftime("%b %-d") if obj.opening_date else "—"
    opening_date_display.short_description = "Opens"
    opening_date_display.admin_order_field = "opening_date"

    def closing_date_display(self, obj):
        return obj.closing_date.strftime("%b %-d") if obj.closing_date else "—"
    closing_date_display.short_description = "Closes"
    closing_date_display.admin_order_field = "closing_date"

    def social_media_display(self, obj):
        url = obj.social_media_url
        if not url:
            return "-"
        if "facebook.com" in url:
            label = "FB"
        elif "instagram.com" in url:
            label = "Insta"
        else:
            label = "other"
        return format_html('<a href="{}" target="_blank" rel="noopener">{}</a>', url, label)
    social_media_display.short_description = "Social"

    def schedule_display(self, obj):
        colors = {"full": "#389e0d", "partial": "#faad14", "none": "#999"}
        labels = {"full": "Full", "partial": "Partial", "none": "None"}
        status = obj.schedule_completeness
        return format_html(
            '<span style="background:{};color:#fff;font-size:.75em;'
            'padding:1px 5px;border-radius:3px">{}</span>',
            colors[status], labels[status],
        )
    schedule_display.short_description = "Schedule"


class PoolAlternateNameInline(admin.TabularInline):
    model = PoolAlternateName
    extra = 1
    verbose_name = "Alternate name"
    verbose_name_plural = "Alternate names"


class ScheduleChangeInline(admin.TabularInline):
    model = ScheduleChange
    extra = 0


class PoolSeasonHistoryInline(admin.TabularInline):
    model = PoolSeasonHistory
    extra = 0
    readonly_fields = ["year", "opening_date", "closing_date"]
    can_delete = False
    ordering = ["-year"]

    def has_add_permission(self, request, obj=None):
        return False


PoolAdmin.inlines = [PoolAlternateNameInline, ScheduleChangeInline, PoolSeasonHistoryInline]


def _source_url(submission):
    """Return the best URL to use as a source attribution for this submission."""
    return submission.url or ""


def apply_to_pool(modeladmin, request, queryset):
    applied = skipped = 0
    applied_pools = []
    reactivated = []
    date_warnings = []
    for sub in queryset.select_related("parsed_pool"):
        if not sub.parsed_pool:
            skipped += 1
            continue

        pool = sub.parsed_pool
        source = _source_url(sub)

        if sub.parsed_opening_date:
            if pool.opening_date and pool.opening_date != sub.parsed_opening_date:
                date_warnings.append(
                    f"{pool.name}: opening date changed from {pool.opening_date} to {sub.parsed_opening_date}"
                )
            pool.opening_date = sub.parsed_opening_date
            pool.opening_date_source_url = source
            if not pool.is_active:
                pool.is_active = True
                reactivated.append(pool.name)
        if sub.parsed_closing_date:
            if pool.closing_date and pool.closing_date != sub.parsed_closing_date:
                date_warnings.append(
                    f"{pool.name}: closing date changed from {pool.closing_date} to {sub.parsed_closing_date}"
                )
            pool.closing_date = sub.parsed_closing_date
            pool.closing_date_source_url = source
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
        applied_pools.append(pool)

    if applied == 1:
        pool = applied_pools[0]
        msg = format_html(
            'Applied 1 submission to <a href="{}">{}</a>.',
            pool.get_absolute_url(),
            pool.name,
        )
    else:
        msg = f"Applied {applied} submission(s) to pool(s)."
    if reactivated:
        msg = format_html("{} Set to active: {}.", msg, ", ".join(reactivated))
    if skipped:
        msg = format_html("{} Skipped {} with no linked pool.", msg, skipped)
    modeladmin.message_user(request, msg)
    for warning in date_warnings:
        modeladmin.message_user(request, f"Date overwrite: {warning}", level=messages.WARNING)

apply_to_pool.short_description = "Apply parsed data to linked pool"


def _suggest_emergency(title):
    if "declares" in title.lower():
        return None
    return (
        HeatHealthEmergency.objects.filter(ends_at__isnull=True).order_by("-starts_at").first()
        or HeatHealthEmergency.objects.order_by("-starts_at").first()
    )


def apply_press_release(modeladmin, request, queryset):
    applied = skipped = 0
    for pr in queryset.order_by("published_at", "detected_at"):
        # Re-resolve here (not just at scrape time) because when applying a batch in one
        # action — e.g. catching up on several historical releases at once — an earlier
        # release in this same batch may have just created the emergency this one acts on.
        emergency = pr.emergency or _suggest_emergency(pr.title)
        if emergency:
            if pr.parsed_starts_at:
                emergency.starts_at = pr.parsed_starts_at
            if pr.parsed_ends_at:
                emergency.ends_at = pr.parsed_ends_at
            elif pr.release_kind == "ends" and not emergency.ends_at:
                # "Ends" release with no specific time parsed — end it as of now rather
                # than leaving it open. If the release simply confirms a date the linked
                # emergency already has, this branch is never hit (true no-op).
                emergency.ends_at = timezone.now()
        else:
            if not pr.parsed_starts_at:
                skipped += 1
                continue
            emergency = HeatHealthEmergency.objects.create(
                starts_at=pr.parsed_starts_at, ends_at=pr.parsed_ends_at
            )
        emergency.save()
        pr.emergency = emergency
        pr.status = "applied"
        pr.reviewed_at = timezone.now()
        pr.save()
        applied += 1
    msg = f"Applied {applied} press release(s)."
    if skipped:
        msg += f" Skipped {skipped} with no linked emergency and no parsed start date — link or correct manually."
    modeladmin.message_user(request, msg)
apply_press_release.short_description = "Apply to emergency (creates new one if unlinked)"


def reject_press_releases(modeladmin, request, queryset):
    updated = queryset.update(status="rejected", reviewed_at=timezone.now())
    modeladmin.message_user(request, f"Rejected {updated} press release(s).")
reject_press_releases.short_description = "Reject selected press releases"


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    change_form_template = "admin/pools/submission_change_form.html"
    show_facets = ShowFacets.ALWAYS
    list_display = ["short_source", "submitted_at", "parsed_pool", "llm_confidence", "status"]
    list_filter = ["status", "llm_confidence"]
    list_select_related = ["parsed_pool"]
    actions = [apply_to_pool]
    list_display_links = ["submitted_at"]
    readonly_fields = [
        "submitted_at",
        "image_preview",
        "raw_fetched_content_display",
        "llm_response_display",
        "date_overwrite_warning",
        "current_opening_date",
        "current_closing_date",
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
                "date_overwrite_warning",
                ("parsed_opening_date", "current_opening_date"),
                ("parsed_closing_date", "current_closing_date"),
                ("parsed_weekday_schedule", "current_weekday_schedule"),
                ("parsed_weekend_schedule", "current_weekend_schedule"),
                ("parsed_notes", "current_updates"),
            ),
        }),
        ("Review", {
            "fields": ("status", "reviewed_at", "moderator_notes"),
        }),
        ("Source content", {
            "classes": ("collapse",),
            "description": "What was fetched, and what the LLM made of it. Submissions from "
                           "structured sources (the city GIS layer) carry the full city record "
                           "here and no LLM response.",
            "fields": ("raw_fetched_content_display", "llm_response_display"),
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
            path("pending-count/",
                 self.admin_site.admin_view(self.pending_count_view),
                 name="pools_submission_pending_count"),
        ] + super().get_urls()

    def pending_count_view(self, request):
        from django.http import JsonResponse
        pending = Submission.objects.filter(status="pending")
        latest_id = pending.order_by("-submitted_at").values_list("id", flat=True).first()
        return JsonResponse({"count": pending.count(), "latest_id": latest_id})

    def response_change(self, request, obj):
        if "_apply" in request.POST:
            apply_to_pool(self, request, Submission.objects.filter(pk=obj.pk))
            if "_set_display_image" in request.POST and obj.uploaded_image and obj.parsed_pool_id:
                Pool.objects.filter(pk=obj.parsed_pool_id).update(display_image_submission=obj)
                self.message_user(request, f"Display image updated for {obj.parsed_pool}.")
            return redirect(request.path)
        if "_reparse" in request.POST:
            self._run_reparse(request, obj)
            return redirect(request.path)
        return super().response_change(request, obj)

    def _run_reparse(self, request, submission):
        from pools.services.llm_parser import parse_submission, parse_image_submission, build_pool_list
        from pools.services.url_fetcher import fetch_url

        pool_list = build_pool_list()
        raw_content = ""
        llm_response = None
        parsed_fields = {}

        if submission.uploaded_image:
            try:
                image_bytes = submission.uploaded_image.read()
                parsed_fields = parse_image_submission(image_bytes, submission.uploaded_image.name, pool_list)
                llm_response = parsed_fields.pop("_raw", None)
            except Exception as e:
                llm_response = {"error": str(e)}
        elif submission.url:
            try:
                raw_content = fetch_url(submission.url)
            except Exception:
                pass
            try:
                parsed_fields = parse_submission(raw_content, pool_list)
                llm_response = parsed_fields.pop("_raw", None)
            except Exception as e:
                llm_response = {"error": str(e)}
        else:
            self.message_user(request, "This submission has no URL or image to parse.", level=messages.WARNING)
            return

        submission.raw_fetched_content = raw_content
        submission.llm_response = llm_response
        if not submission.parsed_pool and parsed_fields.get("pool_id"):
            try:
                submission.parsed_pool = Pool.objects.get(pk=parsed_fields["pool_id"])
            except Pool.DoesNotExist:
                pass
        submission.parsed_opening_date = parsed_fields.get("opening_date")
        submission.parsed_closing_date = parsed_fields.get("closing_date")
        submission.parsed_weekday_schedule = parsed_fields.get("weekday_schedule") or ""
        submission.parsed_weekend_schedule = parsed_fields.get("weekend_schedule") or ""
        parsed_notes = parsed_fields.get("notes") or ""
        if parsed_fields.get("stale_year_warning"):
            parsed_notes = "WARNING: Source may be from a prior season — verify dates before applying.\n" + parsed_notes
        submission.parsed_notes = parsed_notes
        submission.llm_confidence = parsed_fields.get("confidence") or ""
        submission.save()
        self.message_user(request, "Re-parsed with LLM.")

    def apply_view(self, request, submission_id):
        apply_to_pool(self, request, Submission.objects.filter(pk=submission_id))
        return redirect(f"../../{submission_id}/change/")

    def reparse_view(self, request, submission_id):
        submission = get_object_or_404(Submission, pk=submission_id)

        if request.method == "POST" and request.POST.get("action") == "apply":
            # Apply selected pool updates
            source = submission.url or ""
            applied = 0
            reactivated = []
            date_warnings = []
            for pool_id in request.POST.getlist("pool_ids"):
                try:
                    pool = Pool.objects.get(pk=pool_id)
                    opening = request.POST.get(f"opening_date_{pool_id}")
                    closing = request.POST.get(f"closing_date_{pool_id}")
                    notes = request.POST.get(f"notes_{pool_id}", "").strip()
                    phone = request.POST.get(f"phone_number_{pool_id}", "").strip()
                    if opening:
                        new_opening = date.fromisoformat(opening)
                        if pool.opening_date and pool.opening_date != new_opening:
                            date_warnings.append(
                                f"{pool.name}: opening date changed from {pool.opening_date} to {new_opening}"
                            )
                        pool.opening_date = new_opening
                        pool.opening_date_source_url = source
                        if not pool.is_active:
                            pool.is_active = True
                            reactivated.append(pool.name)
                    if closing:
                        new_closing = date.fromisoformat(closing)
                        if pool.closing_date and pool.closing_date != new_closing:
                            date_warnings.append(
                                f"{pool.name}: closing date changed from {pool.closing_date} to {new_closing}"
                            )
                        pool.closing_date = new_closing
                        pool.closing_date_source_url = source
                    if phone:
                        pool.phone_number = phone
                    if notes:
                        existing = pool.updates.strip()
                        pool.updates = (existing + "\n" + notes).strip() if existing else notes
                        pool.updates_source_url = source
                    pool.save()
                    applied += 1
                except (Pool.DoesNotExist, ValueError):
                    pass
            if applied:
                submission.status = "approved"
                submission.reviewed_at = timezone.now()
                submission.save()
            msg = f"Applied updates to {applied} pool(s)."
            if reactivated:
                msg += f" Set to active: {', '.join(reactivated)}."
            messages.success(request, msg)
            for warning in date_warnings:
                messages.warning(request, f"Date overwrite: {warning}")
            if request.POST.get("monitor_page") and submission.url:
                from pools.services.page_monitor import start_monitoring
                page, created, report = start_monitoring(submission.url)
                if not created:
                    messages.info(request, f"Already monitoring {page.url}.")
                elif report.errors:
                    messages.warning(request, f"Now monitoring {page.url}, but the first check failed: {'; '.join(report.errors)}")
                else:
                    messages.success(request, f"Now monitoring {page.url} — baseline content recorded.")
            return redirect(f"../../{submission_id}/change/")

        # GET or POST without action=apply: run the LLM
        results = []
        error = None
        from pools.services.llm_parser import build_pool_list
        pool_list = build_pool_list()
        try:
            if submission.uploaded_image:
                from pools.services.llm_parser import parse_all_pools_image
                results = parse_all_pools_image(submission.uploaded_image.read(), submission.uploaded_image.name, pool_list)
            elif submission.url:
                from pools.services.url_fetcher import fetch_url
                from pools.services.llm_parser import parse_all_pools
                results = parse_all_pools(fetch_url(submission.url), pool_list)
            else:
                error = "This submission has no URL or image to parse."
        except Exception as e:
            error = str(e)

        # Attach matched Pool objects and compute default checkbox state
        pool_by_id = {p.id: p for p in Pool.objects.all()}
        for r in results:
            pool_obj = pool_by_id.get(r.get("pool_id"))
            r["pool_obj"] = pool_obj
            default_checked = True
            if pool_obj:
                parsed_opening = r.get("opening_date")
                parsed_closing = r.get("closing_date")
                parsed_phone = r.get("phone_number", "")
                parsed_notes = r.get("notes", "")
                has_new_data = bool(parsed_phone or parsed_notes)
                if not has_new_data:
                    if parsed_opening and pool_obj.opening_date and pool_obj.opening_date.isoformat() == parsed_opening:
                        default_checked = False
                    if parsed_closing and pool_obj.closing_date and pool_obj.closing_date.isoformat() == parsed_closing:
                        default_checked = False
                    if not pool_obj.is_active and not parsed_opening:
                        default_checked = False
            r["default_checked"] = default_checked

        # Find pools whose dates were sourced from this URL but no longer appear in results
        removed_pools = []
        if submission.url:
            parsed_pool_ids = {r["pool_id"] for r in results if r.get("pool_id")}
            candidates = Pool.objects.filter(
                Q(opening_date_source_url=submission.url, opening_date__isnull=False) |
                Q(closing_date_source_url=submission.url, closing_date__isnull=False)
            ).exclude(pk__in=parsed_pool_ids)
            for pool in candidates:
                stale = []
                if pool.opening_date_source_url == submission.url and pool.opening_date:
                    stale.append(("opening", pool.opening_date))
                if pool.closing_date_source_url == submission.url and pool.closing_date:
                    stale.append(("closing", pool.closing_date))
                removed_pools.append({"pool": pool, "stale": stale})

        return TemplateResponse(request, "admin/pools/submission_reparse.html", {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "submission": submission,
            "results": results,
            "removed_pools": removed_pools,
            "already_monitored": bool(submission.url) and MonitoredPage.objects.filter(url=submission.url).exists(),
            "error": error,
            "title": "Re-parse for all pools",
        })

    def short_source(self, obj):
        if obj.url:
            url = obj.url
            truncated = url[:60] + ("…" if len(url) > 60 else "")
            if "facebook.com" in url:
                badge = mark_safe(
                    '<span style="background:#1877f2;color:#fff;font-size:.75em;'
                    'padding:1px 5px;border-radius:3px;margin-right:4px">FB</span>'
                )
            elif "instagram.com" in url:
                badge = mark_safe(
                    '<span style="background:#e1306c;color:#fff;font-size:.75em;'
                    'padding:1px 5px;border-radius:3px;margin-right:4px">Insta</span>'
                )
            else:
                badge = ""
            return format_html(
                '{}<a href="{}" target="_blank" rel="noopener">{}</a>',
                badge, url, truncated,
            )
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

    def raw_fetched_content_display(self, obj):
        if not obj.raw_fetched_content:
            return "—"
        return format_html(
            "<pre style='white-space:pre-wrap;max-height:400px;overflow:auto'>{}</pre>",
            obj.raw_fetched_content,
        )
    raw_fetched_content_display.short_description = "Fetched source content"

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

    def date_overwrite_warning(self, obj):
        if not obj.parsed_pool:
            return ""
        pool = obj.parsed_pool
        warnings = []
        if obj.parsed_opening_date and pool.opening_date and pool.opening_date != obj.parsed_opening_date:
            warnings.append(
                f"Opening date will change from {pool.opening_date} to {obj.parsed_opening_date}"
            )
        if obj.parsed_closing_date and pool.closing_date and pool.closing_date != obj.parsed_closing_date:
            warnings.append(
                f"Closing date will change from {pool.closing_date} to {obj.parsed_closing_date}"
            )
        if not warnings:
            return ""
        items = format_html_join("", "<li>{}</li>", ((w,) for w in warnings))
        return format_html(
            '<ul style="margin:0;padding-left:1.2em;color:#c0392b;font-weight:bold">{}</ul>',
            items,
        )
    date_overwrite_warning.short_description = "Date overwrite warning"

    def current_opening_date(self, obj):
        return self._current(obj, "opening_date")
    current_opening_date.short_description = "Current opening date"

    def current_closing_date(self, obj):
        return self._current(obj, "closing_date")
    current_closing_date.short_description = "Current closing date"

    def current_weekday_schedule(self, obj):
        return self._current(obj, "weekday_schedule")
    current_weekday_schedule.short_description = "Current weekday schedule"

    def current_weekend_schedule(self, obj):
        return self._current(obj, "weekend_schedule")
    current_weekend_schedule.short_description = "Current weekend schedule"

    def current_updates(self, obj):
        return self._current(obj, "updates")
    current_updates.short_description = "Current updates"


@admin.register(PoolLike)
class PoolLikeAdmin(admin.ModelAdmin):
    list_display = ["pool", "year", "voter_id", "ip_address", "created_at"]
    list_filter = ["year", "pool"]
    readonly_fields = ["pool", "voter_id", "ip_address", "year", "created_at"]

    def has_add_permission(self, request):
        return False


@admin.register(MonitoredPage)
class MonitoredPageAdmin(admin.ModelAdmin):
    list_display = ["url", "page_type", "last_checked", "last_changed", "has_hash"]
    list_filter = ["page_type"]
    readonly_fields = ["content_hash", "last_checked", "last_changed"]

    def has_hash(self, obj):
        # Heat-emergency pages are never diffed, so "initialized" doesn't apply.
        if obj.page_type != "pool_info":
            return "n/a"
        return _boolean_icon(bool(obj.content_hash))
    has_hash.short_description = "Initialized"

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # A new page (or one pointed at a new URL, which clears its baseline) is checked
        # right away rather than waiting for the next cron run.
        if not change or "url" in form.changed_data:
            from pools.services.page_monitor import check_page
            report = check_page(obj)
            level = messages.WARNING if report.errors else messages.INFO
            self.message_user(request, f"First check: {report.summary()}", level=level)


@admin.register(PoolGisState)
class PoolGisStateAdmin(admin.ModelAdmin):
    """Read-only window on what GIS last said, plus the one editable knob that
    matters: clearing a `proposed_*` date makes `check_pool_gis` offer that value
    again on its next run, which is how you undo a rejection you've changed your
    mind about."""
    list_display = ["pool", "gis_status", "gis_opening_date", "proposed_opening_date", "last_checked", "last_changed"]
    list_filter = ["gis_status"]
    list_select_related = ["pool"]
    search_fields = ["pool__name"]
    readonly_fields = ["pool", "gis_status", "gis_opening_date", "gis_closing_date", "last_checked", "last_changed"]

    def has_add_permission(self, request):
        return False  # rows are created by check_pool_gis


@admin.register(DigestState)
class DigestStateAdmin(admin.ModelAdmin):
    list_display = ["__str__", "last_digest_sent_at", "last_error_email_sent_at"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(HeatHealthEmergency)
class HeatHealthEmergencyAdmin(admin.ModelAdmin):
    list_display = ["__str__", "starts_at", "ends_at", "created_at"]
    fields = ["starts_at", "ends_at", "created_at", "updated_at"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(SiteAnnouncement)
class SiteAnnouncementAdmin(admin.ModelAdmin):
    list_display = ["__str__", "color", "show_on_detail_pages", "starts_at", "ends_at", "created_at"]
    fields = ["message", "color", "show_on_detail_pages", "starts_at", "ends_at", "created_at", "updated_at"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(HeatEmergencyPressRelease)
class HeatEmergencyPressReleaseAdmin(admin.ModelAdmin):
    change_form_template = "admin/pools/heatemergencypressrelease_change_form.html"
    list_display = ["title", "published_at", "release_kind", "parsed_starts_at", "parsed_ends_at", "emergency_display", "status"]
    list_filter = ["status", "release_kind"]
    list_select_related = ["emergency"]
    actions = [apply_press_release, reject_press_releases]
    readonly_fields = ["detected_at", "raw_content_display", "llm_response_display"]
    fieldsets = (
        ("Detected press release", {
            "description": "Auto-filled by the scraper, or fill in by hand to manually declare/end an emergency.",
            "fields": ("title", "source_url", "published_at", "detected_at", "raw_content_display"),
        }),
        ("Parsed / editable", {
            "description": "Correct these before applying if the LLM got them wrong. 'emergency' is which "
                           "existing emergency this revises or ends — leave blank to start a new one. It's only "
                           "set in the database once you Apply; until then this just shows what Apply would do.",
            "fields": ("release_kind", "parsed_starts_at", "parsed_ends_at", "emergency"),
        }),
        ("Review", {
            "fields": ("status", "reviewed_at"),
        }),
        ("Raw LLM response", {
            "classes": ("collapse",),
            "fields": ("llm_response_display",),
        }),
    )

    def get_form(self, request, obj=None, **kwargs):
        # ModelForm initial data for a bound instance comes from model_to_dict(obj), which
        # shadows field.initial — so the suggestion has to be set on obj itself to show up
        # pre-selected. Harmless on POST: a real save/apply overwrites this from submitted data.
        if obj and obj.status == "pending" and not obj.emergency_id:
            suggested = _suggest_emergency(obj.title)
            if suggested:
                obj.emergency = suggested
        return super().get_form(request, obj, **kwargs)

    def response_change(self, request, obj):
        if "_apply" in request.POST:
            apply_press_release(self, request, HeatEmergencyPressRelease.objects.filter(pk=obj.pk))
            return redirect(request.path)
        return super().response_change(request, obj)

    def raw_content_display(self, obj):
        return format_html("<pre style='white-space:pre-wrap;max-height:300px;overflow:auto'>{}</pre>", obj.raw_content)
    raw_content_display.short_description = "Fetched page content"

    def llm_response_display(self, obj):
        if obj.llm_response:
            import json
            return format_html("<pre style='white-space:pre-wrap'>{}</pre>", json.dumps(obj.llm_response, indent=2))
        return "—"
    llm_response_display.short_description = "Raw LLM response"

    def emergency_display(self, obj):
        if obj.emergency:
            return str(obj.emergency)
        if obj.status != "pending":
            return "—"
        suggested = _suggest_emergency(obj.title)
        if suggested:
            return format_html("<em>will link to {} on Apply</em>", str(suggested))
        return mark_safe("<em>will create new on Apply</em>")
    emergency_display.short_description = "Emergency"
