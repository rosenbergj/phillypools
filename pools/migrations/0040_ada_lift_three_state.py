# Hand-edited: makemigrations produced a bare RemoveField + AddField, which would have
# dropped the populated column and left every pool back at the default. The add, the
# copy and the remove have to happen in that order for the data to survive.

from django.db import migrations, models


def carry_boolean_over(apps, schema_editor):
    Pool = apps.get_model("pools", "Pool")
    Pool.objects.filter(has_ada_lift=True).update(ada_lift="yes")
    Pool.objects.filter(has_ada_lift=False).update(ada_lift="none")


def back_to_boolean(apps, schema_editor):
    """'broken' means the pool has a lift, so it goes back as True — reversing this
    loses the distinction rather than the pool."""
    Pool = apps.get_model("pools", "Pool")
    Pool.objects.filter(ada_lift__in=["yes", "broken"]).update(has_ada_lift=True)
    Pool.objects.filter(ada_lift="none").update(has_ada_lift=False)


class Migration(migrations.Migration):

    dependencies = [
        ('pools', '0039_usageevent_ada_lift_filter'),
    ]

    operations = [
        migrations.AddField(
            model_name='pool',
            name='ada_lift',
            field=models.CharField(
                choices=[('yes', 'Yes — working'), ('none', 'None'), ('broken', 'Present but broken')],
                default='none',
                help_text="The city's feed only distinguishes a lift from no lift — its 'Y' "
                          "becomes 'yes' and anything else becomes 'none'. 'Present but broken' "
                          "is a human judgement the feed can't express, so scrape_pools never "
                          "sets it and never overwrites it while the city still reports a lift.",
                max_length=10,
                verbose_name='ADA lift',
            ),
        ),
        migrations.RunPython(carry_boolean_over, back_to_boolean),
        migrations.RemoveField(
            model_name='pool',
            name='has_ada_lift',
        ),
    ]
