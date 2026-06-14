from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("pools", "0008_add_monitored_page"),
    ]

    operations = [
        migrations.RenameField(
            model_name="pool",
            old_name="official_website_url",
            new_name="phillypublicpools_url",
        ),
    ]
