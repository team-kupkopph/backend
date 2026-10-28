from django.db import migrations
from django.utils import timezone


def settle(apps, schema_editor):
    """One-time: settle the pledges and needs stranded by accounts deleted before backend#110
    (`community.needs.settle_departed_accounts`). SILENT, idempotent, never touches `quantity_received`.
    The counts are printed so a deploy log records exactly what changed. Preview it first, read-only, with
    `python manage.py report_stranded_pledges`: it asks the same question.

    NOT REVERSIBLE: which rows were stranded is not recorded, so undoing it would leave them settled."""
    from community.needs import settle_departed_accounts
    counts = settle_departed_accounts(
        apps.get_model("accounts", "Account"), apps.get_model("community", "ShelterNeed"),
        apps.get_model("community", "NeedPledge"), timezone.now())
    print(f"\n  community.0009: settled {counts['donor_left']} donor-left pledge(s), "
          f"{counts['need_closed']} need-closed pledge(s), {counts['needs_settled']} need(s)")


class Migration(migrations.Migration):

    dependencies = [
        ("community", "0008_pledge_donor_left"),
        ("accounts", "0010_me_read_model"),
    ]

    operations = [
        migrations.RunPython(settle, migrations.RunPython.noop),
    ]
