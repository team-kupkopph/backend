"""Review step 15 (MO12) · the data half. Anyone who already has more than one open or reviewed report about the same thing gets
them folded into the oldest (the others' words kept, the others dismissed with nobody named), so the constraint in 0007 can be added.

⚠️ A SEPARATE migration from the constraint, on purpose. Postgres refuses `CREATE INDEX` in the same transaction as updates to a
table with deferred foreign-key checks ("cannot CREATE INDEX ... because it has pending trigger events"). In one migration that
would pass on a database with no duplicates and fail on the one that needs the fix. Undoing this is a no-op: the folded rows stay
dismissed, which loses nothing that the collapse had not already lost."""
from django.db import migrations


def fold_existing_duplicates(apps, schema_editor):
    from moderation.reports import merge_live_duplicates
    merge_live_duplicates(apps.get_model("moderation", "ModerationFlag"))


class Migration(migrations.Migration):

    dependencies = [
        ('moderation', '0005_reason_code_and_account_block'),
    ]

    operations = [
        migrations.RunPython(fold_existing_duplicates, migrations.RunPython.noop),
    ]
