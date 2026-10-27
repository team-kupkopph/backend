"""T7 · the `/me` read model and the settings contract (X11, X12, AS8, last_active_at).

  * X12 · `GET /me` 500ed for any account without an `account_settings` row — 29 of 44 in the
    dev DB, every one seeded by a script that bypassed `create_account`. A read must not be the
    place a missing row becomes a server error, and the mobile app hides UI silently on that
    failure, so a broken fixture looked exactly like a passing one.
  * X11 · the city had no read path: the app cached it in SecureStore, so a new phone or a
    reinstall showed "Set your city" while the server had it.
  * AS8 · `approximate_location` and `masked_contact` change nothing (location is city-only
    server-side regardless; masked contact is Phase 2). Two writable booleans that do nothing
    are a contract that lies to the client, so they leave the API.
"""
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.factories import AccountFactory
from accounts.models import AccountSettings, Address
from accounts.tokens import tokens_for


def _hdr(account):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(account)['access']}"}


# ------------------------------------------------------------------------------- X12

@pytest.mark.django_db
def test_me_does_not_500_for_an_account_with_no_settings_row(client):
    account = AccountFactory(email_verified_at=timezone.now())
    AccountSettings.objects.filter(account=account).delete()      # how 29 dev accounts were seeded

    res = client.get("/api/v1/me", **_hdr(account))

    assert res.status_code == 200
    assert res.json()["settings"]["push_enabled"] is True          # the defaults


@pytest.mark.django_db
def test_the_missing_row_is_created_so_the_next_write_has_something_to_update(client):
    account = AccountFactory(email_verified_at=timezone.now())
    AccountSettings.objects.filter(account=account).delete()

    client.get("/api/v1/me", **_hdr(account))

    assert AccountSettings.objects.filter(account=account).count() == 1


@pytest.mark.django_db
def test_the_settings_endpoints_tolerate_a_missing_row_too(client):
    account = AccountFactory(email_verified_at=timezone.now())
    AccountSettings.objects.filter(account=account).delete()

    got = client.get("/api/v1/me/settings", **_hdr(account))
    AccountSettings.objects.filter(account=account).delete()
    patched = client.patch("/api/v1/me/settings", {"marketing_emails": True},
                           content_type="application/json", **_hdr(account))

    assert got.status_code == 200
    assert patched.status_code == 200 and patched.json()["marketing_emails"] is True


# ------------------------------------------------------------------------------- X11

@pytest.mark.django_db
def test_me_returns_the_city_the_user_picked(client):
    account = AccountFactory(email_verified_at=timezone.now())
    client.put("/api/v1/me/location", {"city": "Marikina", "barangay": "Concepcion Uno"},
               content_type="application/json", **_hdr(account))

    body = client.get("/api/v1/me", **_hdr(account)).json()

    assert body["location"] == {"city": "Marikina", "barangay": "Concepcion Uno"}


@pytest.mark.django_db
def test_me_location_is_null_before_a_city_is_chosen(client):
    account = AccountFactory(email_verified_at=timezone.now())

    assert client.get("/api/v1/me", **_hdr(account)).json()["location"] is None


@pytest.mark.django_db
def test_me_location_never_carries_more_than_the_city_and_barangay(client):
    # §12.5 · a person's location is a city, never coordinates or a street address.
    account = AccountFactory(email_verified_at=timezone.now())
    Address.objects.create(account=account, is_primary=True, city="Marikina", barangay="X",
                           line1="12 Real St", province="Metro Manila", postal_code="1800")

    location = client.get("/api/v1/me", **_hdr(account)).json()["location"]

    assert set(location) == {"city", "barangay"}


# ------------------------------------------------------------------------------- AS8

@pytest.mark.django_db
def test_me_settings_carry_only_the_live_switches(client):
    account = AccountFactory(email_verified_at=timezone.now())

    assert set(client.get("/api/v1/me", **_hdr(account)).json()["settings"]) == {
        "push_enabled", "marketing_emails", "analytics_consent"}


@pytest.mark.django_db
def test_settings_endpoint_no_longer_returns_the_two_do_nothing_booleans(client):
    account = AccountFactory(email_verified_at=timezone.now())

    body = client.get("/api/v1/me/settings", **_hdr(account)).json()

    assert "approximate_location" not in body and "masked_contact" not in body
    assert set(body) == {"marketing_emails", "push_enabled", "analytics_consent",
                         "analytics_consent_at"}


@pytest.mark.django_db
def test_an_old_build_that_still_sends_them_gets_a_200_and_nothing_changes(client):
    account = AccountFactory(email_verified_at=timezone.now())

    res = client.patch("/api/v1/me/settings",
                       {"approximate_location": False, "masked_contact": False,
                        "marketing_emails": True},
                       content_type="application/json", **_hdr(account))

    assert res.status_code == 200
    body = res.json()
    assert body["marketing_emails"] is True                  # the real switch still applies
    assert "approximate_location" not in body
    settings_row = AccountSettings.objects.get(account=account)
    assert settings_row.approximate_location is True         # ignored, not applied
    assert settings_row.masked_contact is True


# -------------------------------------------------------------------- last_active_at

def _refresh(client, account):
    token = RefreshToken()
    token["account_id"] = str(account.account_id)
    return client.post("/api/v1/auth/refresh", {"refresh": str(token)},
                       content_type="application/json")


@pytest.mark.django_db
def test_refresh_stamps_last_active_once_a_day_not_on_every_call(client):
    account = AccountFactory(email_verified_at=timezone.now())
    assert account.last_active_at is None

    assert _refresh(client, account).status_code == 200
    account.refresh_from_db()
    first = account.last_active_at
    assert first is not None

    assert _refresh(client, account).status_code == 200
    account.refresh_from_db()
    assert account.last_active_at == first                   # not a write per request


@pytest.mark.django_db
def test_refresh_restamps_after_a_day(client):
    account = AccountFactory(email_verified_at=timezone.now())
    stale = timezone.now() - timedelta(hours=25)
    type(account).objects.filter(pk=account.pk).update(last_active_at=stale)

    assert _refresh(client, account).status_code == 200

    account.refresh_from_db()
    assert account.last_active_at > stale + timedelta(hours=24)


@pytest.mark.django_db
def test_a_refused_refresh_does_not_stamp_activity(client):
    from accounts.models import AccountStatus
    account = AccountFactory(status=AccountStatus.SUSPENDED)

    assert _refresh(client, account).status_code == 401

    account.refresh_from_db()
    assert account.last_active_at is None
