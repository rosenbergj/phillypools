import re

from django.core.management.base import BaseCommand

from pools.models import Pool


def titlecase_address(address):
    def fix_word(word):
        # Split trailing punctuation (e.g. "ST," -> core="ST", suffix=",")
        m = re.match(r'^([A-Za-z0-9]+)([^A-Za-z0-9]*)$', word)
        if not m:
            return word
        core, suffix = m.group(1), m.group(2)
        # Ordinal numbers: 1ST -> 1st, 63RD -> 63rd
        om = re.match(r'^(\d+)(ST|ND|RD|TH)$', core, re.IGNORECASE)
        if om:
            return om.group(1) + om.group(2).lower() + suffix
        # Any word made entirely of ASCII letters (ST, AVE, DR, OLNEY, etc.)
        if re.match(r'^[A-Za-z]+$', core):
            return core[0].upper() + core[1:].lower() + suffix
        return word  # pure numbers, mixed tokens — leave unchanged

    return ' '.join(fix_word(w) for w in address.split())


class Command(BaseCommand):
    help = 'Convert pool addresses from ALL CAPS to Title Case'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write changes to the database (default is dry run)')

    def handle(self, *args, **options):
        dry_run = not options['apply']
        updated = 0
        for pool in Pool.objects.all():
            fixed = titlecase_address(pool.address)
            if fixed != pool.address:
                self.stdout.write(f'  {pool.address!r} -> {fixed!r}')
                if not dry_run:
                    pool.address = fixed
                    pool.save(update_fields=['address'])
                updated += 1
        label = 'Would update' if dry_run else 'Updated'
        self.stdout.write(self.style.SUCCESS(f'{label} {updated} pool(s).'))
        if dry_run:
            self.stdout.write(self.style.WARNING('Dry run — re-run with --apply to write.'))
