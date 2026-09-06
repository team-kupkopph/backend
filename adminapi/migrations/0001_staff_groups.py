"""US-B3 · the two staff roles, as Django Groups.

⚠️ Groups, not a `staff_profile.role` column — and the reason is specific to this repo, not a
general preference. `dev/check-docs.py::check_schema_vs_migrations` scans PROJECT migrations
for `db_table` and skips `.venv`, so `auth_group` (a contrib table) costs nothing. A new column
on `staff_profile` would drag in the full three-artefact parity cycle: Tech Spec §7 DDL ->
regenerate kupkop_mvp_schema.sql byte-identically -> migration, plus a docx regeneration.
Two rows in a table Django already ships buy the same thing for free.

This migration creates no table, so it is invisible to that checker by design.
"""
from django.db import migrations

ROLES = ["reviewer", "superadmin"]


def create_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for name in ROLES:
        Group.objects.get_or_create(name=name)


def drop_groups(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name__in=ROLES).delete()


class Migration(migrations.Migration):
    initial = True
    dependencies = [("auth", "0012_alter_user_first_name_max_length")]
    operations = [migrations.RunPython(create_groups, drop_groups)]
