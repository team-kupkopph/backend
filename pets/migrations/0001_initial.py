# My Pets review step 10 (PP8) · `pets` takes over the existing `pet`, `pet_photo` and
# `pet_ownership_history` tables from `listings`. The models are created in Django's STATE only;
# the only database work is relabelling the three content-type rows, so nothing keyed to them
# (permissions) is orphaned. On a fresh database there is nothing to relabel.

import django.db.models.deletion
import uuid
from django.db import migrations, models


def relabel_content_types(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    moved = 0
    for model in ("pet", "petphoto", "petownershiphistory"):
        if ContentType.objects.filter(app_label="pets", model=model).exists():
            continue
        moved += ContentType.objects.filter(app_label="listings", model=model).update(app_label="pets")
    print(f"\n  pets.0001: relabelled {moved} content type(s) from listings")


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('accounts', '0010_me_read_model'),
        ('listings', '0015_pets_move_out_unlink'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name='Pet',
                    fields=[
                        ('pet_id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('name', models.CharField(max_length=80)),
                        ('species', models.CharField(choices=[('dog', 'Dog'), ('cat', 'Cat'), ('other', 'Other')], max_length=10)),
                        ('breed', models.CharField(blank=True, max_length=80)),
                        ('sex', models.CharField(choices=[('male', 'Male'), ('female', 'Female'), ('unknown', 'Unknown')], default='unknown', max_length=10)),
                        ('date_of_birth', models.DateField(blank=True, null=True)),
                        ('dob_approximate', models.BooleanField(default=False)),
                        ('color_markings', models.CharField(blank=True, max_length=120)),
                        ('weight_kg', models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                        ('size_category', models.CharField(blank=True, choices=[('small', 'Small'), ('medium', 'Medium'), ('large', 'Large')], max_length=10)),
                        ('spayed_neutered', models.BooleanField(blank=True, null=True)),
                        ('microchip_no', models.CharField(blank=True, max_length=40)),
                        ('feeding_routine', models.TextField(blank=True)),
                        ('allergies', models.TextField(blank=True)),
                        ('medications', models.TextField(blank=True)),
                        ('medical_conditions', models.TextField(blank=True)),
                        ('temperament', models.CharField(blank=True, max_length=160)),
                        ('vet_contact', models.CharField(blank=True, max_length=160)),
                        ('emergency_contact', models.CharField(blank=True, max_length=160)),
                        ('removed_at', models.DateTimeField(blank=True, null=True)),
                        ('removed_reason', models.CharField(blank=True, choices=[('rehomed_outside', 'Rehomed Outside'), ('passed_away', 'Passed Away'), ('given_away', 'Given Away'), ('other', 'Other')], max_length=20)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('updated_at', models.DateTimeField(auto_now=True)),
                        ('owner_account', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='pets', to='accounts.account')),
                    ],
                    options={
                        'db_table': 'pet',
                    },
                ),
                migrations.CreateModel(
                    name='PetOwnershipHistory',
                    fields=[
                        ('history_id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('pet_name', models.CharField(blank=True, max_length=80)),
                        ('photo_url', models.TextField(blank=True)),
                        ('transferred_at', models.DateTimeField(auto_now_add=True)),
                        ('from_account', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='accounts.account')),
                        ('listing', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='listings.adoptionlisting')),
                        ('pet', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ownership_history', to='pets.pet')),
                        ('to_account', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='accounts.account')),
                    ],
                    options={
                        'db_table': 'pet_ownership_history',
                    },
                ),
                migrations.CreateModel(
                    name='PetPhoto',
                    fields=[
                        ('photo_id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                        ('url', models.TextField()),
                        ('is_primary', models.BooleanField(default=False)),
                        ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                        ('pet', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='photos', to='pets.pet')),
                    ],
                    options={
                        'db_table': 'pet_photo',
                    },
                ),
                migrations.AddConstraint(
                    model_name='pet',
                    constraint=models.CheckConstraint(condition=models.Q(('name__regex', '\\S')), name='ck_pet_name_not_blank'),
                ),
                migrations.AddConstraint(
                    model_name='pet',
                    constraint=models.CheckConstraint(condition=models.Q(('weight_kg__isnull', True), ('weight_kg__gt', 0), _connector='OR'), name='ck_pet_weight_positive'),
                ),
                migrations.AddConstraint(
                    model_name='petphoto',
                    constraint=models.UniqueConstraint(condition=models.Q(('is_primary', True)), fields=('pet',), name='uq_pet_photo_one_primary'),
                ),
            ],
            database_operations=[],
        ),
        migrations.RunPython(relabel_content_types, migrations.RunPython.noop),
    ]
