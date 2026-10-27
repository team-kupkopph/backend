# T7 · the data cleanups, THEN the rule they make true.
#
# Order matters: the unique index cannot be created while an account still has two primary
# addresses, so duplicates are demoted first. Neither cleanup is reversible in any useful way (a
# demoted address cannot be known to have been primary, a backfilled row is just the defaults), so
# the reverse is a no-op rather than a lie.
from django.db import migrations, models

from accounts.maintenance import backfill_account_settings, demote_duplicate_primary_addresses


def _backfill(apps, schema_editor):
    backfill_account_settings(apps.get_model("accounts", "Account"),
                              apps.get_model("accounts", "AccountSettings"))


def _demote(apps, schema_editor):
    demote_duplicate_primary_addresses(apps.get_model("accounts", "Address"))


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0009_account_anonymized_at'),
    ]

    operations = [
        migrations.RunPython(_backfill, migrations.RunPython.noop),
        migrations.RunPython(_demote, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='address',
            constraint=models.UniqueConstraint(condition=models.Q(('is_primary', True)), fields=('account',), name='uq_address_one_primary'),
        ),
    ]
