from django.db import models


class Pool(models.Model):
    POOL_TYPE_CHOICES = [
        ("outdoor", "Outdoor"),
        ("wading", "Wading"),
        ("spray", "Spray"),
        ("indoor", "Indoor"),
    ]

    name = models.CharField(max_length=200)
    address = models.CharField(max_length=300)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    neighborhood = models.CharField(max_length=100, blank=True)
    official_website_url = models.URLField(blank=True)
    phone_number = models.CharField(max_length=20, blank=True)
    pool_type = models.CharField(max_length=20, choices=POOL_TYPE_CHOICES, default="outdoor")

    opening_date = models.DateField(null=True, blank=True)
    opening_date_source_url = models.URLField(blank=True)
    closing_date = models.DateField(null=True, blank=True)
    closing_date_source_url = models.URLField(blank=True)
    hours = models.CharField(max_length=200, blank=True, help_text='e.g. "11–7 M–F, 12–5 weekends"')
    hours_source_url = models.URLField(blank=True)
    weekday_schedule = models.TextField(blank=True, help_text="One period per line, e.g. '11:00–11:50am: Free swim'")
    weekday_schedule_source_url = models.URLField(blank=True)
    weekend_schedule = models.TextField(blank=True)
    weekend_schedule_source_url = models.URLField(blank=True)

    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def is_open(self):
        from datetime import date
        today = date.today()
        if self.opening_date and self.closing_date:
            return self.opening_date <= today <= self.closing_date
        return None  # unknown


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

    url = models.URLField()
    submitter_note = models.TextField(blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    raw_fetched_content = models.TextField(blank=True)
    llm_response = models.JSONField(null=True, blank=True)

    parsed_pool = models.ForeignKey(Pool, null=True, blank=True, on_delete=models.SET_NULL, related_name="submissions")
    parsed_opening_date = models.DateField(null=True, blank=True)
    parsed_closing_date = models.DateField(null=True, blank=True)
    parsed_hours = models.CharField(max_length=200, blank=True)
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
