"""IB5 / V7 · a badge row carries its rule and its place in the grid.

The thresholds were an `if` ladder in community/badges.py, the display text a free-text seed,
and nothing tied the two. The seeded rows get their rule here; a later badge is one row.
"""
from django.db import migrations, models

RULES = {
    "first_shift": (10, "shifts_completed", 1), "shifts_10": (20, "shifts_completed", 10),
    "shifts_50": (30, "shifts_completed", 50), "first_rescue": (40, "rescues_helped", 1),
    "rehomed_1": (50, "pets_rehomed", 1), "rehomed_5": (60, "pets_rehomed", 5),
    "bayani": (70, "areas_helped", 3),
}


def seed_rules(apps, schema_editor):
    Badge = apps.get_model("community", "Badge")
    for code, (order, metric, threshold) in RULES.items():
        Badge.objects.filter(pk=code).update(sort_order=order, metric=metric, threshold=threshold)


def clear_rules(apps, schema_editor):
    apps.get_model("community", "Badge").objects.filter(pk__in=RULES).update(
        sort_order=0, metric="", threshold=None)


class Migration(migrations.Migration):
    dependencies = [("community", "0009_settle_pledges_of_departed_accounts")]
    operations = [
        migrations.AddField(model_name="badge", name="metric", field=models.CharField(
            blank=True, default="", max_length=20, choices=[
                ("shifts_completed", "Shifts"), ("rescues_helped", "Rescues"),
                ("pets_rehomed", "Rehomed"), ("pledges_delivered", "Pledges"),
                ("areas_helped", "Areas")])),
        migrations.AddField(model_name="badge", name="threshold",
                            field=models.PositiveIntegerField(blank=True, null=True)),
        migrations.AddField(model_name="badge", name="sort_order",
                            field=models.PositiveSmallIntegerField(default=0)),
        migrations.RunPython(seed_rules, clear_rules),
    ]
