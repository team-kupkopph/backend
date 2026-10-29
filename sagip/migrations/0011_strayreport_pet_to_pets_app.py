# My Pets review step 10 (PP8) · stray_report.pet now points at pets.Pet. STATE ONLY: the same
# `pet` table and the same foreign key, no SQL.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pets', '0001_initial'),
        ('sagip', '0010_strayreport_pet_fk'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name='strayreport',
                    name='pet',
                    field=models.ForeignKey(blank=True, db_column='pet_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='lost_reports', to='pets.pet'),
                ),
            ],
            database_operations=[],
        ),
    ]
