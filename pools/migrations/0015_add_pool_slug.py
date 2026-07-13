from django.db import migrations, models
from django.utils.text import slugify


def populate_slugs(apps, schema_editor):
    Pool = apps.get_model("pools", "Pool")
    existing = set(Pool.objects.exclude(slug="").values_list("slug", flat=True))
    for pool in Pool.objects.filter(slug="").order_by("pk"):
        base = slugify(pool.name)
        candidate = base
        n = 2
        while candidate in existing:
            candidate = f"{base}-{n}"
            n += 1
        pool.slug = candidate
        pool.save(update_fields=["slug"])
        existing.add(candidate)


class Migration(migrations.Migration):
    dependencies = [
        ("pools", "0014_pool_display_image"),
    ]

    operations = [
        migrations.AddField(
            model_name="pool",
            name="slug",
            field=models.SlugField(max_length=220, blank=True),
        ),
        # Populate any pools whose slug is still empty.
        migrations.RunPython(populate_slugs, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="pool",
            name="slug",
            field=models.SlugField(max_length=220, unique=True),
        ),
    ]
