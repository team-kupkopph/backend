"""US-B1 / D-S6-2 · seed the badge catalog (shift-agnostic).

Mirrors the `INSERT INTO badge (...)` seed in kupkop_mvp_schema.sql / Tech Spec §7. Idempotent
(update_or_create) so re-running or editing wording later is safe.
"""
from django.db import migrations

BADGES = [
    ("first_shift",  "First Kawang-Gawa",    "Completed your first volunteer shift",     "ti-paw",             "1 completed volunteer shift"),
    ("shifts_10",    "Steady Volunteer",     "Completed 10 volunteer shifts",            "ti-walk",            "10 completed volunteer shifts"),
    ("shifts_50",    "Kawang-Gawa Champion", "Completed 50 volunteer shifts",            "ti-shoe",            "50 completed volunteer shifts"),
    ("first_rescue", "Rescuer",              "Helped your first rescue case",            "ti-heart-handshake", "1 rescue case helped"),
    ("rehomed_1",    "Matchmaker",           "Helped rehome your first pet",             "ti-home-heart",      "1 adoption completed"),
    ("rehomed_5",    "Rehome Hero",          "Helped rehome 5 pets",                     "ti-award",           "5 adoptions completed"),
    ("bayani",       "Bayani",               "Completed shifts, rescues, and a rehome",  "ti-star",            "shift + rescue + rehome"),
]


def seed(apps, schema_editor):
    Badge = apps.get_model("community", "Badge")
    for code, name, description, icon, criteria in BADGES:
        Badge.objects.update_or_create(
            badge_code=code,
            defaults={"name": name, "description": description, "icon": icon,
                      "criteria": criteria})


def unseed(apps, schema_editor):
    Badge = apps.get_model("community", "Badge")
    Badge.objects.filter(badge_code__in=[b[0] for b in BADGES]).delete()


class Migration(migrations.Migration):
    dependencies = [("community", "0002_badge_accountbadge")]
    operations = [migrations.RunPython(seed, unseed)]
