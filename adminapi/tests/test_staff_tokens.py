"""US-B2 · token custody, backend half — lifetime, rotation, and identity isolation."""
from datetime import datetime, timedelta, timezone as dt_timezone

import pytest
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from adminapi.auth import STAFF_REFRESH_LIFETIME
from adminapi.tests.conftest import full_signin


def _exp(token_str, cls):
    """django.utils.timezone.utc was removed in Django 5 — use datetime's own."""
    return datetime.fromtimestamp(cls(token_str)["exp"], tz=dt_timezone.utc)


@pytest.mark.django_db
def test_staff_refresh_lives_eight_hours_not_thirty_days(client, staffer):
    """⚠️ The global SIMPLE_JWT refresh is 30 days — correct for a phone app, wrong for a
    credential that reaches government IDs. One stolen refresh must not grant a month of
    standing platform-admin access."""
    body = full_signin(client, staffer)
    life = _exp(body["refresh"], RefreshToken) - datetime.now(dt_timezone.utc)
    assert STAFF_REFRESH_LIFETIME == timedelta(hours=8)
    assert timedelta(hours=7, minutes=55) < life <= timedelta(hours=8)


@pytest.mark.django_db
def test_the_mobile_refresh_is_still_thirty_days(client):
    """The 8-hour rule is a per-token override, NOT a global edit. Changing the global would
    silently log out every phone, so this pins the other side of that boundary."""
    account = AccountFactory(email="owner@example.com", password="pw", email_verified_at=timezone.now())
    life = _exp(tokens_for(account)["refresh"], RefreshToken) - datetime.now(dt_timezone.utc)
    assert life > timedelta(days=29)


@pytest.mark.django_db
def test_refresh_rotates_and_the_old_one_is_dead(client, staffer):
    first = full_signin(client, staffer)
    res = client.post("/admin-api/auth/refresh", {"refresh": first["refresh"]},
                      content_type="application/json")
    assert res.status_code == 200
    rotated = res.json()
    assert rotated["refresh"] != first["refresh"]

    replay = client.post("/admin-api/auth/refresh", {"refresh": first["refresh"]},
                         content_type="application/json")
    assert replay.status_code == 401


# -- the two identities must not be interchangeable --------------------------------------
@pytest.mark.django_db
def test_a_mobile_refresh_cannot_be_exchanged_for_a_staff_token(client):
    """An Account token has no staff_user_id. If this ever passed, any app user could mint
    platform-admin credentials — so it is asserted rather than assumed."""
    account = AccountFactory(email="owner2@example.com", password="pw", email_verified_at=timezone.now())
    res = client.post("/admin-api/auth/refresh", {"refresh": tokens_for(account)["refresh"]},
                      content_type="application/json")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "not_a_staff_token"


@pytest.mark.django_db
def test_a_mobile_access_token_does_not_authenticate_on_admin_api(client, staffer):
    account = AccountFactory(email="owner3@example.com", password="pw", email_verified_at=timezone.now())
    res = client.post("/admin-api/auth/logout", {"refresh": "x"}, content_type="application/json",
                      HTTP_AUTHORIZATION=f"Bearer {tokens_for(account)['access']}")
    assert res.status_code in (401, 403)


@pytest.mark.django_db
def test_a_staff_access_token_does_not_authenticate_on_the_mobile_api(client, staffer):
    """The mirror of the test above. A staff token has no account_id claim, so
    AccountJWTAuthentication must reject it — the console's credential must not become a
    skeleton key for the app's endpoints."""
    body = full_signin(client, staffer)
    res = client.get("/api/v1/me", HTTP_AUTHORIZATION=f"Bearer {body['access']}")
    assert res.status_code in (401, 403)


@pytest.mark.django_db
def test_removing_the_staff_profile_revokes_access_immediately(client, staffer):
    """Offboarding must not wait for an 8-hour refresh to expire. The bridge is re-resolved on
    every request rather than trusted from the claim."""
    from accounts.models import StaffProfile
    body = full_signin(client, staffer)
    auth = f"Bearer {body['access']}"

    ok = client.post("/admin-api/auth/logout", {"refresh": body["refresh"]},
                     content_type="application/json", HTTP_AUTHORIZATION=auth)
    assert ok.status_code == 204

    # A second sign-in cannot reuse the current code (django_otp's `last_t` replay guard, and
    # both sign-ins fall in the same 30-second step), so the second token pair is minted
    # directly. The subject under test is the permission layer, not the OTP flow.
    from adminapi.auth import tokens_for_staff
    body2 = tokens_for_staff(staffer)
    StaffProfile.objects.filter(user=staffer).delete()
    after = client.post("/admin-api/auth/logout", {"refresh": body2["refresh"]},
                        content_type="application/json",
                        HTTP_AUTHORIZATION=f"Bearer {body2['access']}")
    assert after.status_code in (401, 403)


@pytest.mark.django_db
def test_access_token_alone_is_not_a_refresh(client, staffer):
    body = full_signin(client, staffer)
    res = client.post("/admin-api/auth/refresh", {"refresh": body["access"]},
                      content_type="application/json")
    assert res.status_code == 401
