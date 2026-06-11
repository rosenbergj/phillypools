from django import template
from pools.models import Submission

register = template.Library()


@register.simple_tag
def pending_submission_count():
    return Submission.objects.filter(status="pending").count()
