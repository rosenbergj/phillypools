from django.db import migrations, models


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
    ]
