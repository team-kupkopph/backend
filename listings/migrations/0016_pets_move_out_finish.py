# My Pets review step 10 (PP8) · second half of the move: AdoptionListing's pet links now point at
# pets.Pet and listings forgets its old Pet models. STATE ONLY: the same `pet` table, no SQL.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('listings', '0015_pets_move_out_unlink'),
        ('pets', '0001_initial'),
        ('sagip', '0011_strayreport_pet_to_pets_app'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name='adoptionlisting',
                    name='adopted_pet',
                    field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='pets.pet'),
                ),
                migrations.AlterField(
                    model_name='adoptionlisting',
                    name='rehome_pet',
                    field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='pets.pet'),
                ),
                migrations.RemoveField(
                    model_name='petownershiphistory',
                    name='from_account',
                ),
                migrations.RemoveField(
                    model_name='petownershiphistory',
                    name='listing',
                ),
                migrations.RemoveField(
                    model_name='petownershiphistory',
                    name='to_account',
                ),
                migrations.DeleteModel(
                    name='PetPhoto',
                ),
                migrations.DeleteModel(
                    name='Pet',
                ),
                migrations.DeleteModel(
                    name='PetOwnershipHistory',
                ),
            ],
            database_operations=[],
        ),
    ]
