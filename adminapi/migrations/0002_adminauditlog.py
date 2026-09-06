"""US-D1 · admin_audit_log.

⚠️ This migration is the THIRD of three artefacts, and the order matters. Tech Spec §7 is the
source of truth; `kupkop_mvp_schema.sql` is regenerated from it byte-identically; only then
does the migration land. `dev/check-docs.py::check_schema_vs_migrations` errors on any
`db_table` in a project migration with no CREATE TABLE in the schema SQL — code ahead of docs
is an error by design.
"""
import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("adminapi", "0001_staff_groups"),
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="AdminAuditLog",
            fields=[
                ("audit_id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("actor_label", models.CharField(max_length=150)),
                ("action", models.CharField(max_length=80)),
                ("target_type", models.CharField(max_length=40)),
                ("target_id", models.UUIDField(blank=True, null=True)),
                ("outcome", models.CharField(max_length=20)),
                ("detail", models.JSONField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("actor", models.ForeignKey(blank=True, db_column="actor_id", null=True,
                                            on_delete=django.db.models.deletion.SET_NULL,
                                            related_name="+", to="accounts.account")),
            ],
            options={"db_table": "admin_audit_log"},
        ),
        migrations.AddIndex(
            model_name="adminauditlog",
            index=models.Index(fields=["actor", "-created_at"], name="idx_audit_actor"),
        ),
        migrations.AddIndex(
            model_name="adminauditlog",
            index=models.Index(fields=["target_type", "target_id", "-created_at"], name="idx_audit_target"),
        ),
    ]
