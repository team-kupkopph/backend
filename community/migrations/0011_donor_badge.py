"""IB12 / IQ5 · the donor badge: "Generous", earned when a shelter confirms it received a pledge.

The impact screen counted delivered pledges but no badge used them. Owner decision 2026-11-05: add one, so someone who
helps by giving has something to earn. The rule is the row's own (0010): `pledges_delivered` reaching 1.
"""
from django.db import migrations

BADGE = {
    "badge_code": "first_pledge",
    "name": "Generous",
    "description": "A shelter received your first pledge",
    "icon": "ti-gift",
    "criteria": "1 pledge delivered",
    "metric": "pledges_delivered",
    "threshold": 1,
    "sort_order": 65,      # after the rehome badges, before Bayani (which is shifts + rescues + rehomes)
}


def seed(apps, schema_editor):
    Badge = apps.get_model("community", "Badge")
    code = BADGE["badge_code"]
    Badge.objects.update_or_create(badge_code=code, defaults={k: v for k, v in BADGE.items() if k != "badge_code"})


def unseed(apps, schema_editor):
    # PROTECT stops this once anyone holds it: a held badge is a recorded fact, so reversing is refused rather than erasing it.
    apps.get_model("community", "Badge").objects.filter(pk=BADGE["badge_code"]).delete()


class Migration(migrations.Migration):
    dependencies = [("community", "0010_badge_rules")]
    operations = [migrations.RunPython(seed, unseed)]
