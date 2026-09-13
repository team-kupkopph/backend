import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0009_account_anonymized_at"),
        ("listings", "0004_alter_adoptionlisting_posted_by"),
    ]

    operations = [
        migrations.CreateModel(
            name="ListingPreference",
            fields=[
                ("preference_id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("kind", models.CharField(choices=[("saved", "Saved"), ("hidden", "Hidden")], max_length=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="listing_preferences", to="accounts.account")),
                ("listing", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="preferences", to="listings.adoptionlisting")),
            ],
            options={"db_table": "listing_preference"},
        ),
        migrations.AddConstraint(
            model_name="listingpreference",
            constraint=models.UniqueConstraint(fields=("account", "listing"), name="uq_listing_preference_pair"),
        ),
    ]
