"""Review step 19 · every flag that was already decided had its first (and only) review recorded in reviewed_at/reviewed_by,
so those ARE its first review. Copy them across. Separate from 0010 for the reason 0006/0007 are separate: a data change and
DDL in one transaction on this table can fail with "pending trigger events"."""
from django.db import migrations
from django.db.models import F


def backfill(apps, schema_editor):
    flag = apps.get_model("moderation", "ModerationFlag")
    flag.objects.filter(reviewed_at__isnull=False, first_reviewed_at__isnull=True).update(
        first_reviewed_at=F("reviewed_at"), first_reviewed_by=F("reviewed_by"))


class Migration(migrations.Migration):
    dependencies = [("moderation", "0010_hardening")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
