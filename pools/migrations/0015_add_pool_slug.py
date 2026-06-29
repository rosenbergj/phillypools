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
        # Add the column — idempotent on prod where it was partially created.
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql="ALTER TABLE pools_pool ADD COLUMN IF NOT EXISTS slug varchar(220) NOT NULL DEFAULT '';",
                    reverse_sql="ALTER TABLE pools_pool DROP COLUMN IF EXISTS slug;",
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="pool",
                    name="slug",
                    field=models.SlugField(max_length=220, blank=True),
                ),
            ],
        ),
        # Populate any pools whose slug is still empty.
        migrations.RunPython(populate_slugs, migrations.RunPython.noop),
        # Add unique + varchar_pattern_ops indexes — skip each if it already exists.
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql="""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_index pi
        JOIN pg_attribute pa ON pa.attrelid = pi.indrelid AND pa.attnum = ANY(pi.indkey)
        JOIN pg_class pc ON pc.oid = pi.indrelid
        WHERE pc.relname = 'pools_pool' AND pa.attname = 'slug' AND pi.indisunique
    ) THEN
        CREATE UNIQUE INDEX pools_pool_slug_a5d42396_uniq ON pools_pool (slug);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE tablename = 'pools_pool' AND indexname = 'pools_pool_slug_a5d42396_like'
    ) THEN
        CREATE INDEX pools_pool_slug_a5d42396_like ON pools_pool (slug varchar_pattern_ops);
    END IF;
END $$;
""",
                    reverse_sql="""
DROP INDEX IF EXISTS pools_pool_slug_a5d42396_uniq;
DROP INDEX IF EXISTS pools_pool_slug_a5d42396_like;
""",
                ),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="pool",
                    name="slug",
                    field=models.SlugField(max_length=220, unique=True),
                ),
            ],
        ),
    ]
