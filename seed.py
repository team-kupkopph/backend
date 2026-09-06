import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.utils import timezone

from accounts.models import Account
from listings.models import AdoptionListing
from verifications.models import AccountCapability

acc = Account.objects.filter(email="rescuer@demo.ph").first()
if acc is None:
    acc = Account.objects.create_account(
        account_type="personal", email="rescuer@demo.ph",
        display_name="Demo Rescuer", password=None)
    acc.email_verified_at = timezone.now()
    acc.save(update_fields=["email_verified_at"])
AccountCapability.objects.get_or_create(account=acc, capability="rescuer",
                                        defaults={"status": "approved"})
for name, sp in [("Milo", "dog"), ("Bella", "cat"), ("Coco", "dog")]:
    AdoptionListing.objects.get_or_create(posted_by=acc, name=name,
                                          defaults={"species": sp, "city": "Marikina"})
print("seeded")
