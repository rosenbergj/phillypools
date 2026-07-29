from django.db import models
from django.utils import timezone


class Pool(models.Model):
    POOL_TYPE_CHOICES = [
        ("outdoor", "Outdoor"),
        ("wading", "Wading"),
        ("spray", "Spray"),
        ("indoor", "Indoor"),
    ]

    ppr_amenity_id = models.CharField(max_length=50, unique=True, blank=True, help_text="Stable ID from OpenDataPhilly; used for idempotent imports")
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=300)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    neighborhood = models.CharField(max_length=100, blank=True)
    phillypublicpools_url = models.URLField(blank=True)
    social_media_url = models.URLField(blank=True, help_text="Instagram, Facebook, etc.")
    phone_number = models.CharField(max_length=20, blank=True)
    pool_type = models.CharField(max_length=20, choices=POOL_TYPE_CHOICES, default="outdoor")

    opening_date = models.DateField(null=True, blank=True)
    opening_date_source_url = models.URLField(blank=True)
    closing_date = models.DateField(null=True, blank=True)
    closing_date_source_url = models.URLField(blank=True)
    weekday_schedule = models.TextField(blank=True, help_text="One period per line, e.g. '11:00–11:50am: Free swim'")
    weekday_schedule_source_url = models.URLField(blank=True)
    weekend_schedule = models.TextField(blank=True)
    weekend_schedule_source_url = models.URLField(blank=True)

    notes = models.TextField(blank=True, help_text="Permanent info about this pool (facilities, accessibility, etc.)")
    updates = models.TextField(blank=True, help_text="Current-season updates from submissions or announcements")
    updates_source_url = models.URLField(blank=True)
    is_active = models.BooleanField(default=True)
    last_updated = models.DateTimeField(auto_now=True)

    display_image_submission = models.ForeignKey(
        'Submission',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='displayed_on_pools',
    )
    display_image_caption = models.TextField(blank=True)
    slug = models.SlugField(max_length=220, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify
            base = slugify(self.name)
            candidate = base
            n = 2
            while Pool.objects.filter(slug=candidate).exclude(pk=self.pk).exists():
                candidate = f"{base}-{n}"
                n += 1
            self.slug = candidate
        old_opening = old_closing = None
        if self.pk:
            try:
                old = Pool.objects.get(pk=self.pk)
                old_opening = old.opening_date
                old_closing = old.closing_date
            except Pool.DoesNotExist:
                pass
        super().save(*args, **kwargs)
        _upsert_season_history(self, old_opening=old_opening, old_closing=old_closing)

    def get_absolute_url(self):
        from django.urls import reverse
        return reverse("pool_detail", kwargs={"slug": self.slug})

    @property
    def is_open(self):
        from datetime import date
        today = date.today()
        if not self.is_active:
            return False
        if self.opening_date and self.opening_date <= today:
            return not self.closing_date or self.closing_date >= today
        return None  # no opening date, or opening date in the future

    @property
    def schedule_completeness(self):
        weekday = self.weekday_schedule or ""
        weekend = self.weekend_schedule or ""
        if not weekday.strip() and not weekend.strip():
            return "none"
        if not weekday.strip() or not weekend.strip():
            return "partial"
        if "11" not in weekday or ("6" not in weekday and "7" not in weekday):
            return "partial"
        if "unknown" in weekday.lower() or "unknown" in weekend.lower():
            return "partial"
        return "full"


def _upsert_season_history(pool, old_opening=None, old_closing=None):
    if old_opening and not pool.opening_date:
        PoolSeasonHistory.objects.filter(pool=pool, year=old_opening.year).update(opening_date=None)
        # Only delete if no data worth keeping remains.
        PoolSeasonHistory.objects.filter(
            pool=pool, year=old_opening.year,
            closing_date__isnull=True, weekday_schedule="", weekend_schedule="",
        ).delete()
    if old_closing and not pool.closing_date:
        PoolSeasonHistory.objects.filter(pool=pool, year=old_closing.year).update(closing_date=None)
        PoolSeasonHistory.objects.filter(
            pool=pool, year=old_closing.year,
            opening_date__isnull=True, weekday_schedule="", weekend_schedule="",
        ).delete()

    dates_by_year = {}
    if pool.opening_date:
        dates_by_year.setdefault(pool.opening_date.year, {})["opening_date"] = pool.opening_date
    if pool.closing_date:
        dates_by_year.setdefault(pool.closing_date.year, {})["closing_date"] = pool.closing_date
    for year, fields in dates_by_year.items():
        obj, _ = PoolSeasonHistory.objects.get_or_create(pool=pool, year=year)
        update = dict(fields)
        # Keep schedule history in sync while the pool has schedule data.
        # Never overwrite a non-empty history value with blank.
        if pool.weekday_schedule:
            update["weekday_schedule"] = pool.weekday_schedule
        if pool.weekend_schedule:
            update["weekend_schedule"] = pool.weekend_schedule
        for field, value in update.items():
            setattr(obj, field, value)
        obj.save(update_fields=list(update.keys()))


class PoolSeasonHistory(models.Model):
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="season_history")
    year = models.IntegerField()
    opening_date = models.DateField(null=True, blank=True)
    closing_date = models.DateField(null=True, blank=True)
    weekday_schedule = models.TextField(blank=True)
    weekend_schedule = models.TextField(blank=True)

    class Meta:
        ordering = ["year"]
        unique_together = [("pool", "year")]
        verbose_name_plural = "pool season histories"

    def __str__(self):
        return f"{self.pool.name} {self.year}"


class PoolLike(models.Model):
    """
    An anonymous like, deduplicated by a random voter_id stored in a cookie.
    Scoped to a season `year` (rather than being a single per-pool/voter row) so a
    future season can let people like pools again, while keeping prior years' likes
    around for an all-time count.
    """
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="likes")
    voter_id = models.CharField(max_length=40, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True, help_text="Used only for abuse-rate limiting")
    year = models.IntegerField(help_text="Season year this like was cast in")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("pool", "voter_id", "year")]

    def __str__(self):
        return f"Like on {self.pool.name} ({self.year})"


class ScheduleChange(models.Model):
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="schedule_changes")
    date_from = models.DateField()
    date_to = models.DateField(null=True, blank=True, help_text="Leave blank for an open-ended change with no known end date")
    description = models.TextField()
    source_url = models.URLField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date_from"]

    def __str__(self):
        return f"{self.pool.name}: {self.date_from} – {self.date_to or 'ongoing'}"


class PoolAlternateName(models.Model):
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="alternate_names")
    name = models.CharField(max_length=200, help_text="Nickname or abbreviation used in community sources (e.g. 'MARC Pool')")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.pool.name})"


class MonitoredPage(models.Model):
    """A URL checked periodically — pool-info pages are diffed for content changes
    (creating a Submission); heat-emergency pages are scanned for new DPH press releases."""
    PAGE_TYPE_CHOICES = [
        ("pool_info", "Pool info"),
        ("heat_emergency", "Heat emergency info"),
    ]

    url = models.URLField(unique=True)
    page_type = models.CharField(max_length=20, choices=PAGE_TYPE_CHOICES, default="pool_info")
    content_hash = models.CharField(max_length=64, blank=True)
    last_checked = models.DateTimeField(null=True, blank=True)
    last_changed = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if self.pk:
            try:
                old = MonitoredPage.objects.get(pk=self.pk)
                if old.url != self.url:
                    self.content_hash = ""
                    self.last_checked = None
                    self.last_changed = None
            except MonitoredPage.DoesNotExist:
                pass
        super().save(*args, **kwargs)

    def __str__(self):
        return self.url


class Submission(models.Model):
    CONFIDENCE_CHOICES = [
        ("high", "High"),
        ("medium", "Medium"),
        ("low", "Low"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("hold", "Hold"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    url = models.URLField(blank=True)
    uploaded_image = models.FileField(upload_to="submissions/", blank=True)
    submitter_note = models.TextField(blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    raw_fetched_content = models.TextField(blank=True)
    llm_response = models.JSONField(null=True, blank=True)

    parsed_pool = models.ForeignKey(Pool, null=True, blank=True, on_delete=models.SET_NULL, related_name="submissions")
    parsed_opening_date = models.DateField(null=True, blank=True)
    parsed_closing_date = models.DateField(null=True, blank=True)
    parsed_weekday_schedule = models.TextField(blank=True)
    parsed_weekend_schedule = models.TextField(blank=True)
    parsed_notes = models.TextField(blank=True)
    llm_confidence = models.CharField(max_length=10, choices=CONFIDENCE_CHOICES, blank=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    moderator_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-submitted_at"]

    def __str__(self):
        return f"{self.url} ({self.submitted_at:%Y-%m-%d})"


class HeatHealthEmergency(models.Model):
    """A single Philadelphia-declared Heat Health Emergency, with a start and (once known) end."""
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Blank while still in effect / not yet confirmed ended.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-starts_at"]
        verbose_name_plural = "Heat health emergencies"

    def __str__(self):
        end = timezone.localtime(self.ends_at).strftime("%b %-d, %-I%p") if self.ends_at else "ongoing"
        return f"{timezone.localtime(self.starts_at).strftime('%b %-d, %-I%p')} – {end}"

    def latest_press_release(self):
        """The most recently published press release acting on this emergency (a revision, if any, wins)."""
        return self.press_releases.order_by("-published_at", "-detected_at").first()


class HeatEmergencyPressRelease(models.Model):
    """A detected Philadelphia Dept. of Public Health press release about a Heat Health Emergency."""
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("applied", "Applied"),
        ("rejected", "Rejected"),
    ]

    title = models.CharField(max_length=255)
    source_url = models.URLField(unique=True)
    raw_content = models.TextField(blank=True)
    detected_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateField(
        null=True, blank=True,
        help_text="Date the press release itself was published.",
    )

    RELEASE_KIND_CHOICES = [
        ("declares_or_extends", "Declares / extends"),
        ("ends", "Ends"),
    ]
    release_kind = models.CharField(
        max_length=20, choices=RELEASE_KIND_CHOICES, default="declares_or_extends",
        help_text="What this specific release announces — not whether an emergency is "
                   "currently in effect.",
    )
    parsed_starts_at = models.DateTimeField(null=True, blank=True)
    parsed_ends_at = models.DateTimeField(null=True, blank=True)
    llm_response = models.JSONField(null=True, blank=True)

    emergency = models.ForeignKey(
        HeatHealthEmergency, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="press_releases",
        help_text="Which emergency this release acts on (a revision or an end). "
                   "Leave blank to start a brand-new emergency when applied.",
    )

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-published_at", "-detected_at"]

    def __str__(self):
        return f"{self.title} ({self.detected_at:%Y-%m-%d})"


class SiteAnnouncement(models.Model):
    """An admin-created, site-wide banner shown for a fixed time window (e.g. a maintenance notice)."""
    COLOR_CHOICES = [
        ("cyan", "Cyan"),
        ("yellow", "Yellow"),
        ("orange", "Orange"),
        ("red", "Red"),
    ]
    message = models.TextField(
        help_text="Shown in the banner on every page. Markdown links and bold/italic/code "
                   "are supported; other HTML is stripped."
    )
    color = models.CharField(max_length=10, choices=COLOR_CHOICES, default="red")
    show_on_detail_pages = models.BooleanField(
        default=True,
        help_text="Also show on individual pool detail pages (always shown on the main page).",
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Leave blank to keep showing indefinitely.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-starts_at"]

    def __str__(self):
        starts = timezone.localtime(self.starts_at).strftime("%b %-d, %-I%p")
        ends = timezone.localtime(self.ends_at).strftime("%b %-d, %-I%p") if self.ends_at else "ongoing"
        return f"{self.message[:50]} ({starts} – {ends})"


class DigestState(models.Model):
    """Singleton row tracking when notification emails were last sent."""
    last_digest_sent_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Pending items created after this are 'new' in the next digest.",
    )
    last_error_email_sent_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Scraper errors alone trigger at most one email per day.",
    )

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return "Digest state"


class VisitorSalt(models.Model):
    """
    The random salt used to pseudonymize today's visitors (see pools/services/usage.py).

    Exactly one row exists at a time — writing today's salt deletes every other
    day's — so a stolen database, or the offseason backup, never contains the salt
    needed to link a stored visitor hash back to an IP address.
    """
    day = models.DateField(unique=True)
    salt = models.CharField(max_length=64)

    def __str__(self):
        return f"Visitor salt for {self.day}"


class UsageEvent(models.Model):
    """
    One recorded interaction. Deliberately holds no IP, user-agent or full referrer:
    see the module docstring in pools/services/usage.py.

    These rows are raw material, pruned after USAGE_RAW_RETENTION_DAYS. Anything
    meant to survive the season goes into UsageDaily.
    """
    EVENT_CHOICES = [
        ("index", "Main page"),
        ("pool_view", "Pool detail page"),
        ("filter", "Filter / search (JSON fetch)"),
        ("map_pick", "Neighborhood picked on map"),
        ("pin_click", "Map pin clicked"),
        ("card_click", "Pool clicked in the list"),
        ("pageview_js", "Page loaded (browser confirmed)"),
        ("submit_view", "Submit form viewed"),
        ("submit_done", "Submission completed"),
        ("probe", "Scanner probe (404)"),
        ("legacy_id", "Reached a pool by its retired numeric ID, with no referrer"),
        ("other", "Other page"),
    ]

    created_at = models.DateTimeField(auto_now_add=True)
    day = models.DateField(db_index=True, help_text="Local date, so grouping needs no timezone math")
    event = models.CharField(max_length=20, choices=EVENT_CHOICES, db_index=True)
    key = models.CharField(max_length=100, blank=True, help_text="Pool slug for pool views and pin clicks")
    status_filter = models.CharField(max_length=20, blank=True)
    neighborhood = models.CharField(max_length=100, blank=True)
    zip_searched = models.CharField(max_length=5, blank=True)
    visitor = models.CharField(max_length=16, db_index=True, help_text="Daily-rotating pseudonym; not linkable across days")
    client_class = models.CharField(max_length=10, default="unknown", help_text="'bot', 'staff' or 'unknown' — never a positive 'human' claim")
    ua_family = models.CharField(max_length=20, blank=True, help_text="What the user-agent claimed to be, e.g. 'chrome/129' — the claim, not the verdict, and never the string itself")
    datacenter = models.BooleanField(default=False, help_text="Whether the request came from a hosting provider's address range. The address itself is read once and never stored")
    device = models.CharField(max_length=10, blank=True)
    referrer_host = models.CharField(max_length=100, blank=True, help_text="Host only, and only for external referrers")

    class Meta:
        indexes = [models.Index(fields=["day", "event"])]

    def __str__(self):
        return f"{self.day} {self.event} {self.key}".strip()


class UsageDaily(models.Model):
    """
    Permanent, aggregate-only usage history: counts per day, with no visitor
    pseudonyms. This is the table that survives the offseason backup and makes
    year-over-year comparison possible.

    Long format (metric + key) rather than fixed columns so a new breakdown is a
    rollup change rather than a migration.
    """
    day = models.DateField(db_index=True)
    metric = models.CharField(max_length=32, help_text="e.g. 'visitors', 'pool_view', 'status_filter', 'zip'")
    key = models.CharField(max_length=100, blank=True, help_text="The specific pool slug, filter value, zip, etc.")
    audience = models.CharField(
        max_length=10,
        default="human",
        help_text="Who was counted: 'human' is every visitor the crawler check let "
                  "through, 'confirmed' only those whose browser ran the page's JavaScript",
    )
    events = models.IntegerField(default=0, help_text="Number of interactions")
    visitors = models.IntegerField(default=0, help_text="Number of distinct visitors that day")

    class Meta:
        unique_together = [("day", "metric", "key", "audience")]
        ordering = ["-day", "metric", "-visitors"]

    def __str__(self):
        return f"{self.day} {self.metric}:{self.key} ({self.audience}) = {self.visitors}"


class UsageRollupState(models.Model):
    """
    Singleton row recording when the usage rollup last ran, so /stats/ can say how
    current its numbers are.

    Tracked separately from the UsageDaily rows rather than inferred from them: a
    pass that finds no new traffic still means the figures are current as of now,
    and a timestamp derived from the counts themselves would instead freeze at the
    last time anybody visited.
    """
    last_run_at = models.DateTimeField(null=True, blank=True)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f"Usage rollup last run: {self.last_run_at or 'never'}"
