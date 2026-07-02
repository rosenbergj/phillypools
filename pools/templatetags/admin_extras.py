from django import template
from pools.models import HeatEmergencyPressRelease, Submission

register = template.Library()


@register.simple_tag
def pending_submission_count():
    return Submission.objects.filter(status="pending").count()


@register.simple_tag
def pending_press_release_count():
    return HeatEmergencyPressRelease.objects.filter(status="pending").count()
