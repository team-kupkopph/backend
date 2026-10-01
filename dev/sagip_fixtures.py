"""Seed the personas of the Sagip test plan (dev/test-plan-sagip.md §2, tooling ask T1).

    python dev/sagip_fixtures.py          # every persona except RES-N, plus the backfill
    python dev/sagip_fixtures.py RES-N    # only the named handles (RES-N is created mid-run)

Personas sign in through `POST /api/v1/auth/dev/seed_tokens {"email": …}` (DEBUG only), so
none of them has a password. STAFF is a Django staff user with an unusable password; mint
its admin-api token in a shell with `adminapi.auth.tokens_for_staff(user)`.

Re-running resets each persona to the plan's STARTING standing (status, phone, primary
city, capability, verification, the escalation-partner flag) without duplicating rows. That
is what the start of a run wants; mid-run it undoes ST-2 and RA-6's set-up, so don't.

⚠️ WHAT THIS DELIBERATELY REFUSES TO DO
  · run outside DEBUG;
  · touch any persona address that is not `.invalid` (RFC 2606: it can never collide with a
    real person's and can never receive mail);
  · give a persona a phone number another account already holds (`account.phone` is unique).

It also backfills `AccountSettings` for every account missing one: accounts seeded by
scripts that bypassed `create_account` have none, and `GET /me` 500s for them.

Report-time alerts, matching and notifications only fire through `POST /reports`, so the
run files its own reports through the API; this seeds people, not reports.
"""
import os
import sys

import django

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from accounts.models import Account, AccountSettings, Address, StaffProfile  # noqa: E402
from listings.models import Pet, PetPhoto  # noqa: E402
from shelter.models import OrgType, ShelterProfile, ShelterTier  # noqa: E402
from verifications.models import AccountCapability, VerificationRequest  # noqa: E402

MARIKINA, PASIG, CEBU = "Marikina City", "Pasig City", "Cebu City"

# Handle → standing. `phone` set means verified; `shelter` is the shelter_org verification
# status (None = no request at all, i.e. an unverified shelter).
PERSONAS = {
    "REP":    {"email": "sg.reporter@kupkop.invalid", "city": MARIKINA, "phone": "+639175550101"},
    "REP-U":  {"email": "sg.reporter.unverified@kupkop.invalid", "city": MARIKINA},
    "REP-C":  {"email": "sg.reporter.cap@kupkop.invalid", "city": MARIKINA, "phone": "+639175550103"},
    "REP-T":  {"email": "sg.reporter.throttle@kupkop.invalid", "city": MARIKINA,
               "phone": "+639175550104"},
    "OWN":    {"email": "sg.owner@kupkop.invalid", "city": MARIKINA, "phone": "+639175550105"},
    "FND":    {"email": "sg.finder@kupkop.invalid", "city": PASIG, "phone": "+639175550106"},
    "HLP":    {"email": "sg.helper@kupkop.invalid", "city": MARIKINA, "phone": "+639175550107"},
    "RES-A":  {"email": "sg.rescuer.a@kupkop.invalid", "city": MARIKINA, "phone": "+639175550108",
               "rescuer": True},
    "RES-B":  {"email": "sg.rescuer.b@kupkop.invalid", "city": MARIKINA, "phone": "+639175550109",
               "rescuer": True},
    "RES-N":  {"email": "sg.rescuer.new@kupkop.invalid", "city": MARIKINA, "rescuer": True},
    "RES-P":  {"email": "sg.rescuer.pasig@kupkop.invalid", "city": PASIG, "phone": "+639175550111",
               "rescuer": True},
    "RES-S":  {"email": "sg.rescuer.suspended@kupkop.invalid", "city": MARIKINA, "rescuer": True,
               "status": "suspended"},
    "SHL-V":  {"email": "sg.shelter@kupkop.invalid", "city": MARIKINA, "shelter": "approved",
               "org_name": "Sagip Test Shelter"},
    "SHL-PT": {"email": "sg.partner@kupkop.invalid", "city": PASIG, "shelter": "approved",
               "org_name": "Sagip Partner Shelter"},
    "SHL-X":  {"email": "sg.shelter.cebu@kupkop.invalid", "city": CEBU, "shelter": "approved",
               "org_name": "Sagip Cebu Shelter"},
    # The plan named e2e.shelter@kupkop.invalid, but dev/e2e_fixtures.py approves that one
    # (flow 30 needs a public shift), so an unverified shelter needs its own account.
    "SHL-U":  {"email": "sg.shelter.unverified@kupkop.invalid", "city": MARIKINA, "shelter": None,
               "org_name": "Sagip Unverified Shelter"},
}
# RES-N exists to be a rescuer nobody has paged yet (SW-12), so it is created mid-run only.
DEFAULT_HANDLES = [h for h in PERSONAS if h != "RES-N"]

STAFF_USERNAME = "sg.staff"
STAFF_EMAIL = "sg.staff@kupkop.invalid"

BRUNO = {"species": "dog", "breed": "Aspin", "color_markings": "brown, white chest",
         "size_category": "medium", "sex": "male"}
BRUNO_PHOTOS = ["https://example.invalid/sagip/bruno-1.jpg",
                "https://example.invalid/sagip/bruno-2.jpg"]


def refuse(message):
    print(f"REFUSING: {message}", file=sys.stderr)
    raise SystemExit(1)


def _display_name(handle):
    return f"Sagip {handle}"


def _person(handle):
    spec = PERSONAS[handle]
    email = spec["email"]
    if not email.endswith(".invalid"):
        refuse(f"{email} is not a .invalid address")
    is_shelter = "shelter" in spec
    account = Account.objects.filter(email=email).first() or Account.objects.create_account(
        account_type="shelter" if is_shelter else "personal", email=email,
        display_name=_display_name(handle))

    phone = spec.get("phone")
    if phone and Account.objects.filter(phone=phone).exclude(pk=account.pk).exists():
        refuse(f"{phone} already belongs to another account")
    now = timezone.now()
    account.phone = phone
    account.phone_verified_at = (account.phone_verified_at or now) if phone else None
    account.email_verified_at = account.email_verified_at or now
    account.status = spec.get("status", "active")
    account.save()
    AccountSettings.objects.get_or_create(account=account)

    Address.objects.filter(account=account, is_primary=True).exclude(city=spec["city"]).update(
        is_primary=False)
    if not Address.objects.filter(account=account, is_primary=True).exists():
        Address.objects.create(account=account, city=spec["city"], is_primary=True)

    if spec.get("rescuer"):
        AccountCapability.objects.update_or_create(
            account=account, capability="rescuer",
            defaults={"status": "approved", "granted_at": now})

    if is_shelter:
        ShelterProfile.objects.update_or_create(account=account, defaults={
            "org_name": spec["org_name"], "org_type": OrgType.SHELTER,
            "tier": ShelterTier.REGISTERED_NGO, "is_escalation_partner": False})
        requests = VerificationRequest.objects.filter(account=account, type="shelter_org")
        if spec["shelter"] is None:
            requests.delete()
        elif not requests.exists():
            VerificationRequest.objects.create(account=account, type="shelter_org",
                                               status=spec["shelter"], reviewed_at=now)
        else:
            requests.update(status=spec["shelter"])
    return account


def _bruno(owner):
    pet, _ = Pet.objects.update_or_create(owner_account=owner, name="Bruno", defaults=BRUNO)
    if sorted(pet.photos.values_list("url", flat=True)) != sorted(BRUNO_PHOTOS):
        pet.photos.all().delete()
        for i, url in enumerate(BRUNO_PHOTOS):
            PetPhoto.objects.create(pet=pet, url=url, is_primary=(i == 0))


def _staff():
    account = Account.objects.filter(email=STAFF_EMAIL).first() or Account.objects.create_account(
        account_type="admin", email=STAFF_EMAIL, display_name="Sagip staff")
    user = User.objects.filter(username=STAFF_USERNAME).first()
    if user is None:
        user = User(username=STAFF_USERNAME, email=STAFF_EMAIL)
    user.is_staff, user.is_active = True, True
    user.set_unusable_password()
    user.save()
    StaffProfile.objects.update_or_create(user=user, defaults={"account": account})
    return user


def backfill_settings():
    missing = Account.objects.filter(settings__isnull=True)
    count = 0
    for account in missing:
        AccountSettings.objects.get_or_create(account=account)
        count += 1
    return count


def seed(handles=None):
    if not settings.DEBUG:
        refuse("DEBUG is off — this writes test personas into the connected database")
    unknown = [h for h in (handles or []) if h not in PERSONAS]
    if unknown:
        refuse(f"unknown handle(s): {', '.join(unknown)}")
    with transaction.atomic():
        seeded = {h: _person(h) for h in (handles or DEFAULT_HANDLES)}
        if "OWN" in seeded:
            _bruno(seeded["OWN"])
        if not handles:
            _staff()
        backfilled = backfill_settings()
    return seeded, backfilled


def main(argv):
    seeded, backfilled = seed(argv or None)
    for handle, account in seeded.items():
        print(f"{handle:7} {account.email:42} {account.pk}")
    if not argv:
        print(f"{'STAFF':7} {STAFF_USERNAME:42} (django staff user; tokens_for_staff in a shell)")
    print(f"# {len(seeded)} persona(s) seeded; AccountSettings backfilled for {backfilled} "
          "account(s)", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])
