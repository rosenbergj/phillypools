"""Email digest of items awaiting review, sent via AWS SES.

An email goes out when either:
- at least one pending Submission or HeatEmergencyPressRelease was created since
  the last digest email, or
- the scrapers reported errors (rate-limited to one error-triggered email per day,
  so a persistently broken scraper doesn't email on every cron run).

Called from `run_url_watcher` after the checks run. Also callable on its own via
the `send_pending_digest` command, so notification can later move to a separate
cron decoupled from the scrape schedule.
"""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from pools.models import DigestState, HeatEmergencyPressRelease, Submission

SITE_BASE_URL = "https://phillypools.app"
ERROR_EMAIL_MIN_INTERVAL = timedelta(hours=24)


def send_digest_if_needed(scrape_errors=None, dry_run=False, out=None):
    """Send the digest email if warranted. Returns a status string for logging.

    `out`, if given, is a callable (e.g. Command.stdout.write) used for dry-run
    and not-configured output.
    """
    scrape_errors = scrape_errors or []
    out = out or print
    now = timezone.now()
    state = DigestState.load()

    pending_subs = list(Submission.objects.filter(status="pending").order_by("submitted_at"))
    pending_prs = list(
        HeatEmergencyPressRelease.objects.filter(status="pending").order_by("detected_at")
    )

    since = state.last_digest_sent_at
    new_subs = [s for s in pending_subs if since is None or s.submitted_at > since]
    new_prs = [p for p in pending_prs if since is None or p.detected_at > since]

    items_trigger = bool(new_subs or new_prs)
    errors_trigger = bool(scrape_errors) and (
        state.last_error_email_sent_at is None
        or now - state.last_error_email_sent_at >= ERROR_EMAIL_MIN_INTERVAL
    )
    if not items_trigger and not errors_trigger:
        return "no email needed"

    subject, body = _build_email(pending_subs, pending_prs, new_subs, new_prs, scrape_errors)

    if dry_run:
        out(f"[dry run] Subject: {subject}\n\n{body}")
        return "dry run — email not sent, state not updated"

    if not _configured():
        out(
            "SES not configured (need SES_ACCESS_KEY_ID, SES_SECRET_ACCESS_KEY, "
            "DIGEST_FROM_EMAIL, DIGEST_TO_EMAIL) — printing digest instead:\n\n"
            f"Subject: {subject}\n\n{body}"
        )
        return "SES not configured — email not sent, state not updated"

    _send_via_ses(subject, body)

    if items_trigger:
        state.last_digest_sent_at = now
    if scrape_errors:
        # Any sent email that carried errors resets the error-email clock.
        state.last_error_email_sent_at = now
    state.save()
    return f"email sent to {settings.DIGEST_TO_EMAIL}"


def _configured():
    return all([
        settings.SES_ACCESS_KEY_ID,
        settings.SES_SECRET_ACCESS_KEY,
        settings.DIGEST_FROM_EMAIL,
        settings.DIGEST_TO_EMAIL,
    ])


def _build_email(pending_subs, pending_prs, new_subs, new_prs, scrape_errors):
    subject_parts = []
    if new_subs:
        subject_parts.append(f"{len(new_subs)} new submission{'s' if len(new_subs) != 1 else ''}")
    if new_prs:
        subject_parts.append(
            f"{len(new_prs)} new heat release{'s' if len(new_prs) != 1 else ''}"
        )
    if scrape_errors and not subject_parts:
        subject_parts.append("scraper errors")
    subject = "PhillyPools: " + ", ".join(subject_parts)

    lines = []

    if new_subs:
        lines.append("New submissions:")
        for s in new_subs:
            pool = s.parsed_pool.name if s.parsed_pool else "(no pool matched)"
            confidence = s.llm_confidence or "no"
            lines.append(f"- {pool} — {confidence} confidence — {s.url or s.submitter_note}")
            lines.append(f"  {SITE_BASE_URL}/admin/pools/submission/{s.pk}/change/")
        lines.append("")

    if new_prs:
        lines.append("New heat emergency press releases:")
        for p in new_prs:
            lines.append(f"- {p.title} ({p.get_release_kind_display()})")
            lines.append(f"  {SITE_BASE_URL}/admin/pools/heatemergencypressrelease/{p.pk}/change/")
        lines.append("")

    older_subs = len(pending_subs) - len(new_subs)
    older_prs = len(pending_prs) - len(new_prs)
    if older_subs or older_prs:
        parts = []
        if older_subs:
            parts.append(f"{older_subs} older submission{'s' if older_subs != 1 else ''}")
        if older_prs:
            parts.append(f"{older_prs} older press release{'s' if older_prs != 1 else ''}")
        lines.append(f"Also still pending: {', '.join(parts)}.")
        lines.append("")

    if scrape_errors:
        lines.append("Scraper errors this run:")
        lines.extend(f"- {e}" for e in scrape_errors)
        lines.append("")

    lines.append(f"Pending queue: {SITE_BASE_URL}/admin/pools/submission/?status__exact=pending")
    return subject, "\n".join(lines)


def _send_via_ses(subject, body):
    import boto3

    client = boto3.client(
        "sesv2",
        region_name=settings.SES_REGION,
        aws_access_key_id=settings.SES_ACCESS_KEY_ID,
        aws_secret_access_key=settings.SES_SECRET_ACCESS_KEY,
    )
    client.send_email(
        FromEmailAddress=settings.DIGEST_FROM_EMAIL,
        Destination={"ToAddresses": [settings.DIGEST_TO_EMAIL]},
        Content={
            "Simple": {
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": body}},
            }
        },
    )
