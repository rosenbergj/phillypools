from django.db import migrations, models


def seed_monitored_page(apps, schema_editor):
    MonitoredPage = apps.get_model("pools", "MonitoredPage")
    MonitoredPage.objects.create(
        url="https://www.phila.gov/2026-06-09-philadelphia-2026-public-pool-opening-schedule/",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("pools", "0007_add_pool_season_history"),
    ]

    operations = [
        migrations.CreateModel(
            name="MonitoredPage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("url", models.URLField(unique=True)),
                ("content_hash", models.CharField(blank=True, max_length=64)),
                ("last_checked", models.DateTimeField(blank=True, null=True)),
                ("last_changed", models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.RunPython(seed_monitored_page, migrations.RunPython.noop),
    ]
