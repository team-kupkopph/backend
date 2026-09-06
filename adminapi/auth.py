"""Staff authentication for the platform-ops console (Sprint 9 US-B1).

Deliberately separate from `accounts/` — the two identities are different things and
conflating them is how a reviewer decision ends up attributed to nobody:

* `accounts.tokens.tokens_for` mints an **Account** token for the mobile app. Its claim is
  `account_id` and `AccountJWTAuthentication` resolves it to an `Account`.
* This module mints a **staff** token. A Kupkop reviewer is a Django `contrib.auth.User`
  (that is what `/admin` and `django_otp` know about), but every decision they record is
  attributed to an `Account(account_type='admin')` through `StaffProfile`. So the token
  carries BOTH ids, and `IsStaffJWT` resolves both on every request.

⚠️ TOTP is mandatory and there is no password-only path. `config/settings.py` records the
rule as "a phished password alone must never be enough to reach gov IDs" — these endpoints
reach government IDs, so the rule carries over verbatim. The existing `otp_totp.TOTPDevice`
rows are reused unchanged; only the admin-site gate is replaced.
"""
from datetime import timedelta

from django.contrib.auth import authenticate
from django_otp.plugins.otp_totp.models import TOTPDevice
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.staff import reviewer_account

# ⚠️ EIGHT HOURS, not the thirty days in settings.SIMPLE_JWT.
#
# That 30-day refresh is right for a phone app you do not want to log people out of. It is
# wrong for a credential that reaches government IDs: one stolen refresh would grant a month
# of standing platform-admin access. Today's Django admin session expires in ONE hour
# (SESSION_COOKIE_AGE = 3600), so eight hours is already a loosening — one workday, with TOTP
# re-auth each morning.
#
# Applied per token rather than by changing the global, because the mobile app depends on the
# 30 days and a global edit would silently log out every phone.
STAFF_REFRESH_LIFETIME = timedelta(hours=8)

# The login->verify handoff. Short by design: it is a half-authenticated state.
CHALLENGE_LIFETIME = timedelta(minutes=5)


class StaffAuthError(Exception):
    """Raised with a stable code the view turns into a response."""

    def __init__(self, code: str, status: int = 401):
        super().__init__(code)
        self.code = code
        self.status = status


def verified_totp_device(user):
    """The user's confirmed TOTP device, or None.

    Only `confirmed=True` devices count: an unconfirmed device is one someone started
    enrolling and never proved they could read, so accepting it would let an interrupted
    enrolment stand in for a second factor.
    """
    return TOTPDevice.objects.filter(user=user, confirmed=True).first()


def start_login(email: str, password: str):
    """Step 1. Returns the authenticated Django user, or raises StaffAuthError.

    Every failure below returns the same `invalid_credentials` code on purpose. Sign-in must
    not reveal whether an email belongs to a staff account (§12.1's enumeration asymmetry:
    signup may say "this email already exists", sign-in may not).
    """
    user = authenticate(username=email, password=password)
    if user is None or not user.is_active or not user.is_staff:
        raise StaffAuthError("invalid_credentials")

    if verified_totp_device(user) is None:
        # A staffer with no confirmed device cannot complete a sign-in, and saying so is safe:
        # by this point the password was correct, so no enumeration is possible.
        raise StaffAuthError("totp_not_enrolled", status=403)

    return user


def tokens_for_staff(user):
    """Mint the staff token pair. Raises if the staff bridge is missing.

    ⚠️ A staff user with no `StaffProfile` is REFUSED a token. `accounts/staff.py` states the
    contract: None "means a setup error the caller must surface, never silently stamp an
    anonymous review". A token without `admin_account_id` would let a decision be written with
    a null `reviewed_by` — the one outcome the whole staff bridge exists to prevent.
    """
    account = reviewer_account(user)
    if account is None:
        raise StaffAuthError("staff_profile_missing", status=403)

    refresh = RefreshToken()
    refresh["staff_user_id"] = user.id
    refresh["admin_account_id"] = str(account.account_id)
    refresh.set_exp(lifetime=STAFF_REFRESH_LIFETIME)

    access = refresh.access_token
    access["staff_user_id"] = user.id
    access["admin_account_id"] = str(account.account_id)

    return {"access": str(access), "refresh": str(refresh)}


# --------------------------------------------------------------------------------------
# The login -> verify-otp handoff
# --------------------------------------------------------------------------------------
#
# The challenge is a signed, expiring token binding step 2 to the user who passed step 1.
# It carries no secret: possessing it proves only that a correct password was presented.
#
# ⚠️ ON "SINGLE USE". The obvious implementation is a one-shot nonce in the cache — but this
# project configures no CACHES, so Django falls back to LocMemCache, which is PER PROCESS.
# Under more than one worker a nonce written by one process is invisible to the next, so the
# guarantee would be fictional exactly where it matters (production) while looking real in
# development. That is worse than not claiming it.
#
# The control that actually holds is DB-backed and already shipped: `TOTPDevice.verify_token`
# records `last_t` and refuses a code it has already accepted, and django_otp's own throttling
# counts failures on the device row. So a replayed challenge buys an attacker nothing — the
# code they replay with it is dead, and a fresh code needs the authenticator they do not have.
#
# If a shared cache (Redis) is ever configured, make the challenge one-shot here too and
# delete this note.
from django.core import signing

_CHALLENGE_SALT = "adminapi.staff-login-challenge"


def issue_challenge(user) -> str:
    return signing.dumps({"uid": user.id}, salt=_CHALLENGE_SALT)


def resolve_challenge(challenge: str):
    """Return the user id the challenge was issued for, or raise StaffAuthError."""
    try:
        data = signing.loads(challenge, salt=_CHALLENGE_SALT,
                             max_age=int(CHALLENGE_LIFETIME.total_seconds()))
    except signing.SignatureExpired:
        raise StaffAuthError("challenge_expired")
    except signing.BadSignature:
        raise StaffAuthError("invalid_challenge")
    return data["uid"]


def verify_totp(user, code: str) -> None:
    """Raise StaffAuthError unless `code` is currently valid for the user's device."""
    device = verified_totp_device(user)
    if device is None:
        raise StaffAuthError("totp_not_enrolled", status=403)
    if not device.verify_token(code):
        # django_otp refuses a code it has already accepted (`last_t`) and counts failures on
        # the device row, so this covers replay and brute force without extra machinery.
        raise StaffAuthError("invalid_code")
