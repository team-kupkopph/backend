"""Create or refresh the throwaway accounts the Maestro suite signs in with.

The E2E README says the flows use "a throwaway account on a dev/staging backend, created by
the developer running this". Until now that creation was a sequence of commands somebody
typed once, which is not a setup anyone else can reproduce — and reproducing it is the whole
point of the suite.

Run it, then eval what it prints:

    python dev/e2e_fixtures.py            # create/refresh, print the exports
    eval "$(python dev/e2e_fixtures.py)"  # ...straight into the environment

⚠️ WHAT THIS DELIBERATELY REFUSES TO DO
  · run outside DEBUG — these are accounts with known passwords;
  · touch any address that is not `.invalid` (RFC 2606: permanently unresolvable, so the
    address can never collide with a real person's and can never receive mail);
  · reuse a password — every run generates new ones, so a leaked value expires by itself.

⚠️ THE ACCOUNTS ARE NOT BARE. Both need standing a real user would have earned, or the flows
fail on gates that are working correctly:
  · the owner needs `adopter` + a verified phone, because the adoption inquiry is gated on
    Verified Member and a verified number (US-A3/A4). A bare account is refused, and that
    refusal is right — the fixture is what has to change;
  · the shelter needs a `shelter_profile` row, or the shelter shell has nothing to render.

⚠️ LOGIN IS RATE-LIMITED AND FRESH FIXTURES DO NOT HELP — verified the hard way. The endpoint
carries BOTH `LoginIpThrottle` and `LoginIdentifierThrottle` (common/throttles.py), so a burst
of local runs trips the IP limit and every account from this machine is refused, new ones
included. Rotating passwords here does nothing for it either. HTTP 429 with ~30 minutes to
wait is the backend working exactly as designed; space the runs out, which is also how the
pre-release job uses them.
"""
import os
import secrets
import sys

import django

# Same bootstrap as dev/load_check.py: this runs from dev/, so the project root has to go on
# the path before `config.settings` can be imported.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings  # noqa: E402
from django.utils import timezone  # noqa: E402

from accounts.models import Account  # noqa: E402
from shelter.models import ShelterProfile  # noqa: E402
from volunteer.models import VolunteerShift  # noqa: E402
from verifications.models import AccountCapability  # noqa: E402

OWNER = "e2e.owner@kupkop.invalid"
SHELTER = "e2e.shelter@kupkop.invalid"


def refuse(message):
    print(f"REFUSING: {message}", file=sys.stderr)
    raise SystemExit(1)


def ensure(email, account_type, display_name):
    if not email.endswith(".invalid"):
        refuse(f"{email} is not a .invalid address")
    password = secrets.token_urlsafe(18)
    account = Account.objects.filter(email=email).first()
    if account is None:
        account = Account.objects.create_account(
            account_type=account_type, email=email,
            display_name=display_name, password=password)
    else:
        account.set_password(password)
        account.save(update_fields=["password_hash"])
    if not account.email_verified_at:
        account.email_verified_at = timezone.now()
    account.status = "active"
    account.save()
    return account, password


def main():
    if not settings.DEBUG:
        refuse("DEBUG is off — this creates accounts with known passwords")

    owner, owner_pw = ensure(OWNER, "personal", "E2E owner")
    # Standing a real adopter would have. Without it the inquiry is correctly refused.
    for capability in ("adopter", "rescuer"):
        AccountCapability.objects.update_or_create(
            account=owner, capability=capability, defaults={"status": "approved"})
    if not owner.phone_verified_at:
        owner.phone = owner.phone or "+639170000123"
        owner.phone_verified_at = timezone.now()
        owner.save(update_fields=["phone", "phone_verified_at"])

    shelter, shelter_pw = ensure(SHELTER, "shelter", "E2E shelter")
    ShelterProfile.objects.get_or_create(
        account=shelter, defaults={"org_name": "E2E Test Shelter", "tier": 1})

    # An OPEN shift in the future, because 30-volunteer-signup browses for one and there is
    # nothing to sign up for otherwise. The flow failing on an empty list is technically
    # correct — a flow that skips itself when its fixture is missing is the green-that-checked-
    # nothing failure US-G1 was written about — but "correct failure" is not coverage, so the
    # fixture provides the row rather than the flow tolerating its absence.
    starts = timezone.now() + timezone.timedelta(days=3)
    if not VolunteerShift.objects.filter(shelter_account=shelter, status="open",
                                         starts_at__gte=timezone.now()).exists():
        VolunteerShift.objects.create(
            shelter_account=shelter, type="walking", capacity=5, status="open",
            starts_at=starts, ends_at=starts + timezone.timedelta(hours=2))

    # stdout is the exports and nothing else, so `eval "$(...)"` is safe.
    print(f"export MAESTRO_EMAIL={OWNER}")
    print(f"export MAESTRO_PASSWORD={owner_pw}")
    print(f"export MAESTRO_SHELTER_EMAIL={SHELTER}")
    print(f"export MAESTRO_SHELTER_PASSWORD={shelter_pw}")
    print("# fixtures refreshed — passwords are new, the previous pair no longer works",
          file=sys.stderr)


if __name__ == "__main__":
    main()
