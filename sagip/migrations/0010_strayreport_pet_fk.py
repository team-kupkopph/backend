# PP16 (My Pets review 2026-10-28) · stray_report.pet_id becomes the foreign key the DDL always had.
#
# The column keeps its name, type and values: only Django's idea of the field changes (a UUIDField
# called pet_id becomes a ForeignKey called pet over the same pet_id column), so the state half and
# the database half are written separately. Left to makemigrations this would have dropped the
# column and re-added it empty, losing every lost report's link to its pet.

import django.db.models.deletion
from django.db import migrations, models


def unlink_reports_whose_pet_is_gone(apps, schema_editor):
    """A report can name a pet that no longer exists (deleted before this link was enforced). Clear
    those ids so the constraint can be added, and say how many (the community.0009 pattern)."""
    StrayReport = apps.get_model("sagip", "StrayReport")
    Pet = apps.get_model("listings", "Pet")
    gone = (StrayReport.objects.filter(pet_id__isnull=False)
            .exclude(pet_id__in=Pet.objects.values("pet_id")))
    count = gone.update(pet_id=None)
    print(f"\n  sagip.0010: unlinked {count} report(s) whose pet no longer exists")


class Migration(migrations.Migration):

    dependencies = [
        ("listings", "0014_pet_constraints"),
        ("sagip", "0009_strayreport_alert_held"),
    ]

    operations = [
        migrations.RunPython(unlink_reports_whose_pet_is_gone, migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(model_name="strayreport", name="pet_id"),
                migrations.AddField(
                    model_name="strayreport", name="pet",
                    field=models.ForeignKey(blank=True, db_column="pet_id", null=True,
                                            on_delete=django.db.models.deletion.SET_NULL,
                                            related_name="lost_reports", to="listings.pet"),
                ),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql=[
                        "ALTER TABLE stray_report ADD CONSTRAINT stray_report_pet_id_fk_pet_pet_id "
                        "FOREIGN KEY (pet_id) REFERENCES pet (pet_id) DEFERRABLE INITIALLY DEFERRED",
                        "CREATE INDEX stray_report_pet_id_idx ON stray_report (pet_id)",
                    ],
                    reverse_sql=[
                        "DROP INDEX stray_report_pet_id_idx",
                        "ALTER TABLE stray_report DROP CONSTRAINT stray_report_pet_id_fk_pet_pet_id",
                    ],
                ),
            ],
        ),
    ]
