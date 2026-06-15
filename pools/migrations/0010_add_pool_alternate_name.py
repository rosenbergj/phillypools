import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("pools", "0009_rename_official_website_url"),
    ]

    operations = [
        migrations.CreateModel(
            name="PoolAlternateName",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(help_text="Nickname or abbreviation used in community sources (e.g. 'MARC Pool')", max_length=200)),
                ("pool", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="alternate_names", to="pools.pool")),
            ],
            options={
                "ordering": ["name"],
            },
        ),
    ]
