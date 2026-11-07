"""Review step 7a (MQ10) · the Terms of Use MECHANISM: a published version, a way to accept it, and a gate on posting that is OFF until
counsel's text exists. These hold the mechanism; the text is the owner's.

Google Play's UGC policy wants terms accepted before posting. Apple 1.2 expects the developer's own terms to define objectionable content."""
import pytest
from django.test import override_settings

from accounts.factories import AccountFactory
from listings.tests.adoption_helpers import c, listing, person
from volunteer.tests.helpers import verified_shelter

TERMS = "/api/v1/terms"
ME = "/api/v1/me/terms"
CURRENT = "2026-12-01"


def _stale(account, version="2026-08-01"):
    account.terms_consent_version = version
    account.save(update_fields=["terms_consent_version"])
    return account


def _story(who):
    return c(who).post("/api/v1/stories", {"caption": "hello", "story_type": "general", "photos": [{"file_url": "https://x/p.jpg"}]},
                       format="json")


# ---- published, and public ---------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_the_current_version_is_public_and_says_whether_posting_needs_it(client):
    body = client.get(TERMS).json()
    assert set(body) == {"version", "enforced"} and body["enforced"] is False
    with override_settings(TERMS_VERSION=CURRENT, TERMS_ENFORCED=True):
        assert client.get(TERMS).json() == {"version": CURRENT, "enforced": True}


# ---- accepting ------------------------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_accepting_the_current_version_records_it_and_when():
    with override_settings(TERMS_VERSION=CURRENT, TERMS_ENFORCED=True):
        who = _stale(person())
        before = c(who).get(ME).json()
        assert before["needs_acceptance"] is True and before["accepted_version"] == "2026-08-01"
        res = c(who).post(ME, {"version": CURRENT}, format="json")
        assert res.status_code == 200 and res.json()["accepted_version"] == CURRENT and res.json()["needs_acceptance"] is False
        who.refresh_from_db()
        assert who.terms_consent_version == CURRENT and who.terms_consent_at is not None


@pytest.mark.django_db
@pytest.mark.parametrize("body", [{"version": "2026-08-01"}, {"version": 5}, {"version": None}, {}, [], "x"])
def test_accepting_anything_but_the_current_version_is_refused_and_records_nothing(body):
    with override_settings(TERMS_VERSION=CURRENT):
        who = _stale(person())
        res = c(who).post(ME, body, format="json")
        assert res.status_code == 409 and res.json()["error"]["code"] == "terms_version_changed"
        assert res.json()["error"]["current_version"] == CURRENT
        who.refresh_from_db()
        assert who.terms_consent_version == "2026-08-01"


@pytest.mark.django_db
def test_a_guest_cannot_accept(client):
    assert client.post(ME, {"version": "x"}, content_type="application/json").status_code in (401, 403)


# ---- the gate -----------------------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_with_enforcement_off_nobody_is_stopped_whatever_they_accepted():
    with override_settings(TERMS_VERSION=CURRENT, TERMS_ENFORCED=False):
        assert _story(_stale(person())).status_code == 201


@pytest.mark.django_db
def test_with_enforcement_on_a_stale_acceptance_cannot_post_and_is_told_why():
    with override_settings(TERMS_VERSION=CURRENT, TERMS_ENFORCED=True):
        who = _stale(person())
        res = _story(who)
        assert res.status_code == 403 and res.json()["error"]["code"] == "terms_not_accepted"
        c(who).post(ME, {"version": CURRENT}, format="json")
        assert _story(who).status_code == 201


@pytest.mark.django_db
def test_every_kind_of_post_is_gated_the_same_way():
    with override_settings(TERMS_VERSION=CURRENT, TERMS_ENFORCED=True):
        poster = _stale(person())
        live = listing(person())
        shelter = _stale(verified_shelter())
        calls = [
            c(poster).post("/api/v1/listings", {"pet": {"name": "Bantay", "species": "dog"}, "city": "Marikina", "adoption_fee": "0"}, format="json"),
            c(poster).post(f"/api/v1/listings/{live.pk}/inquiries", {"message": "hi"}, format="json"),
            c(shelter).post("/api/v1/shelter/shifts", {"type": "walking", "title": "Walk", "starts_at": "2030-01-01T09:00:00+08:00",
                                                       "ends_at": "2030-01-01T11:00:00+08:00", "capacity": 2}, format="json"),
        ]
        assert [r.json()["error"]["code"] for r in calls] == ["terms_not_accepted"] * 3
        assert [r.status_code for r in calls] == [403] * 3


@pytest.mark.django_db
def test_reporting_blocking_reading_and_a_rescue_report_are_never_gated():
    """An animal in trouble, and a person trying to report abuse, outrank a document."""
    with override_settings(TERMS_VERSION=CURRENT, TERMS_ENFORCED=True):
        who = _stale(person())
        target = listing(person())
        assert c(who).post("/api/v1/moderation/flags", {"target_type": "listing", "target_id": str(target.pk), "reason_code": "spam_scam"},
                           format="json").status_code == 201
        assert c(who).post("/api/v1/me/blocks", {"account_id": str(AccountFactory().pk)}, format="json").status_code == 201
        assert c(who).get("/api/v1/stories").status_code == 200
        assert c(who).post("/api/v1/reports", {"lat": 14.65, "lng": 121.1, "city": "Marikina", "species": "dog", "condition": "injured"},
                           format="json").status_code == 201


@pytest.mark.django_db
def test_signing_up_records_the_current_version_so_a_new_member_is_never_asked_at_once():
    with override_settings(TERMS_VERSION=CURRENT, TERMS_ENFORCED=True):
        who = AccountFactory(terms_consent_version=CURRENT)
        assert _story(who).status_code == 201


def test_the_gate_is_off_by_default_in_the_shipped_settings():
    """Until counsel's text exists the gate must not be on, or every member would be made to accept a document nobody reviewed.
    A source read, not a reload of the settings module: reloading it mid-run would re-run its side effects."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "config" / "settings.py").read_text()
    assert len(src) > 1000                                                         # it read the real file
    assert 'TERMS_ENFORCED = os.environ.get("TERMS_ENFORCED", "0") == "1"' in src
