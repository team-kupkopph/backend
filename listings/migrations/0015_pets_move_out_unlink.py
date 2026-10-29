# My Pets review step 10 (PP8) · the pet tables move from `listings` to the new `pets` app.
# STATE ONLY: no SQL runs (the tables, columns and constraints are untouched). This first half
# drops listings' pet foreign keys from Django's state so `pets` can be created without a cycle.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('listings', '0014_pet_constraints'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(
                    model_name='pet',
                    name='owner_account',
                ),
                migrations.RemoveField(
                    model_name='petownershiphistory',
                    name='pet',
                ),
                migrations.RemoveField(
                    model_name='petphoto',
                    name='pet',
                ),
            ],
            database_operations=[],
        ),
    ]
