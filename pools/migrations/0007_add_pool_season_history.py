from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("pools", "0006_remove_hours_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="PoolSeasonHistory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("pool", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="season_history", to="pools.pool")),
                ("year", models.IntegerField()),
                ("opening_date", models.DateField(blank=True, null=True)),
                ("closing_date", models.DateField(blank=True, null=True)),
            ],
            options={
                "ordering": ["year"],
                "unique_together": {("pool", "year")},
            },
        ),
    ]
