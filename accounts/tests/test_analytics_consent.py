"""US-N3/N5 · analytics consent — the column D-S6-6 deferred out of Sprint 6.

§17 gates client-side/behavioural analytics on consent and §12.6 puts the burden on the
controller to DEMONSTRATE it was given, which is why the flag ships with a timestamp rather
than alone. Sprint 6's US-Y1 could only instrument account-free server-side outcomes
precisely because this column did not exist.

The posture (D-S7-3): opt-in, default OFF, and it gates ONLY the client-side events. The
server-authoritative aggregate events keep flowing either way — they carry no PII, which is
the whole reason they need no consent.
"""
import pytest

from accounts.factories import AccountFactory
from accounts.models import AccountSettings
from accounts.tokens import tokens_for


def _auth(account):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(account)['access']}"}


@pytest.mark.django_db
def test_consent_is_off_until_someone_opts_in(client):
    account = AccountFactory()
    res = client.get("/api/v1/me/settings", **_auth(account))

    assert res.status_code == 200
    assert res.json()["analytics_consent"] is False
    assert res.json()["analytics_consent_at"] is None


@pytest.mark.django_db
def test_opting_in_stamps_when_consent_was_given(client):
    account = AccountFactory()
    res = client.patch("/api/v1/me/settings", {"analytics_consent": True},
                       content_type="application/json", **_auth(account))

    assert res.status_code == 200
    assert res.json()["analytics_consent"] is True
    # The timestamp is the demonstrability half (§12.6) — a bare boolean cannot show WHEN.
    assert res.json()["analytics_consent_at"] is not None


@pytest.mark.django_db
def test_withdrawing_consent_clears_the_stamp(client):
    # Withdrawal is half of what consent means. The stamp records a CURRENT consent, so
    # leaving an old timestamp behind on a withdrawn flag would misrepresent the record.
    account = AccountFactory()
    client.patch("/api/v1/me/settings", {"analytics_consent": True},
                 content_type="application/json", **_auth(account))
    res = client.patch("/api/v1/me/settings", {"analytics_consent": False},
                       content_type="application/json", **_auth(account))

    assert res.json()["analytics_consent"] is False
    assert res.json()["analytics_consent_at"] is None


@pytest.mark.django_db
def test_re_consenting_restamps_rather_than_keeping_the_first_time(client):
    account = AccountFactory()
    auth = _auth(account)
    client.patch("/api/v1/me/settings", {"analytics_consent": True},
                 content_type="application/json", **auth)
    first = client.get("/api/v1/me/settings", **auth).json()["analytics_consent_at"]
    client.patch("/api/v1/me/settings", {"analytics_consent": False},
                 content_type="application/json", **auth)
    client.patch("/api/v1/me/settings", {"analytics_consent": True},
                 content_type="application/json", **auth)
    second = client.get("/api/v1/me/settings", **auth).json()["analytics_consent_at"]

    assert second is not None and second != first


@pytest.mark.django_db
def test_patching_an_unrelated_setting_leaves_the_consent_stamp_alone(client):
    account = AccountFactory()
    auth = _auth(account)
    client.patch("/api/v1/me/settings", {"analytics_consent": True},
                 content_type="application/json", **auth)
    before = client.get("/api/v1/me/settings", **auth).json()["analytics_consent_at"]

    client.patch("/api/v1/me/settings", {"push_enabled": False},
                 content_type="application/json", **auth)

    after = client.get("/api/v1/me/settings", **auth).json()["analytics_consent_at"]
    assert after == before


@pytest.mark.django_db
def test_re_sending_the_same_consent_value_does_not_move_the_stamp(client):
    # An idempotent PATCH is not a new act of consent.
    account = AccountFactory()
    auth = _auth(account)
    client.patch("/api/v1/me/settings", {"analytics_consent": True},
                 content_type="application/json", **auth)
    before = client.get("/api/v1/me/settings", **auth).json()["analytics_consent_at"]

    client.patch("/api/v1/me/settings", {"analytics_consent": True},
                 content_type="application/json", **auth)

    assert client.get("/api/v1/me/settings", **auth).json()["analytics_consent_at"] == before


@pytest.mark.django_db
def test_the_column_defaults_off_at_the_model_level_too(client):
    account = AccountFactory()
    settings_row = AccountSettings.objects.get(account=account)
    assert settings_row.analytics_consent is False
    assert settings_row.analytics_consent_at is None
