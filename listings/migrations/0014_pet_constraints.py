# PP15 · the constraints themselves, a migration after the cleanup (0013): Postgres refuses an
# ALTER TABLE in the same transaction as row updates that left foreign-key checks pending.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('listings', '0013_pet_constraint_cleanup'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='adoptionlisting',
            constraint=models.UniqueConstraint(condition=models.Q(('status__in', ['draft', 'available', 'pending'])), fields=('rehome_pet',), name='uq_adoption_listing_one_open_rehome'),
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
    ]
