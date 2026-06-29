from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('pools', '0013_season_history_schedule'),
    ]

    operations = [
        migrations.AddField(
            model_name='pool',
            name='display_image_submission',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='displayed_on_pools',
                to='pools.submission',
            ),
        ),
        migrations.AddField(
            model_name='pool',
            name='display_image_caption',
            field=models.TextField(blank=True),
        ),
    ]
