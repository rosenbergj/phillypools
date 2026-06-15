from django.db import models


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

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        _upsert_season_history(self)

    @property
    def is_open(self):
        from datetime import date
        today = date.today()
        if not self.is_active:
            return False
        if self.opening_date and self.opening_date <= today:
            return not self.closing_date or self.closing_date >= today
        return None  # no opening date, or opening date in the future


def _upsert_season_history(pool):
    """Update PoolSeasonHistory whenever opening_date or closing_date is set on a pool."""
    dates_by_year = {}
    if pool.opening_date:
        dates_by_year.setdefault(pool.opening_date.year, {})["opening_date"] = pool.opening_date
    if pool.closing_date:
        dates_by_year.setdefault(pool.closing_date.year, {})["closing_date"] = pool.closing_date
    for year, fields in dates_by_year.items():
        obj, _ = PoolSeasonHistory.objects.get_or_create(pool=pool, year=year)
        for field, value in fields.items():
            setattr(obj, field, value)
        obj.save(update_fields=list(fields.keys()))


class PoolSeasonHistory(models.Model):
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="season_history")
    year = models.IntegerField()
    opening_date = models.DateField(null=True, blank=True)
    closing_date = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["year"]
        unique_together = [("pool", "year")]

    def __str__(self):
        return f"{self.pool.name} {self.year}"


class ScheduleChange(models.Model):
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="schedule_changes")
    date_from = models.DateField()
    date_to = models.DateField()
    description = models.TextField()
    source_url = models.URLField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date_from"]

    def __str__(self):
        return f"{self.pool.name}: {self.date_from} – {self.date_to}"


class PoolAlternateName(models.Model):
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="alternate_names")
    name = models.CharField(max_length=200, help_text="Nickname or abbreviation used in community sources (e.g. 'MARC Pool')")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.pool.name})"


class MonitoredPage(models.Model):
    """A URL whose main content is periodically checked for changes."""
    url = models.URLField(unique=True)
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
