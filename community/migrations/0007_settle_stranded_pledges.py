from django.db import migrations
from django.utils import timezone


def settle(apps, schema_editor):
    """One-time: cancel the `pledged` pledges left on needs closed before the refactor's cascade
    (`community.needs.settle_stranded_pledges`). No notifications. Not reversible: which pledges
    were stranded is not recorded, so undoing it leaves them settled."""
    from community.needs import settle_stranded_pledges
    settle_stranded_pledges(apps.get_model("community", "NeedPledge"), timezone.now())


class Migration(migrations.Migration):

    dependencies = [
        ("community", "0006_wishlist_refactor"),
    ]

    operations = [
        migrations.RunPython(settle, migrations.RunPython.noop),
    ]
