"""US-SEC5 — prove `is_anonymous` actually holds: with it set, the reporter's identity
(account_id, email, display_name) must appear in NO response to any other caller, across
every endpoint that could echo it — report detail, map, offers, the claimer's case view,
and notification payloads. `is_anonymous` only hides the reporter from *other users*
(sagip.models.StrayReport's own docstring); the reporter themselves and staff are out of
scope for this sweep — this is about what OTHER accounts and guests can see.

This sweep holds today by construction: no sagip view ever serializes reporter identity
fields (grep sagip/views.py — reporter_account_id/email/display_name never appear in a
response body). These tests lock that property in as a regression guard, not a fix.
"""

import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from sagip.models import StrayReport
from verifications.models import AccountCapability

LAT, LNG = 14.6507, 121.1029  # Marikina


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _identity_strings(reporter):
    return [str(reporter.pk), reporter.email, reporter.display_name]


def _assert_no_leak(body, reporter):
    text = str(body)
    for needle in _identity_strings(reporter):
        assert needle not in text, f"reporter identity leaked: {needle!r} in {text!r}"


def _verified_member():
    acc = AccountFactory()
    AccountCapability.objects.create(account=acc, capability="rescuer", status="approved",
                                     granted_at=timezone.now())
    return acc


def _anon_report(reporter, **kw):
    defaults = dict(reporter_account=reporter, is_anonymous=True, species="dog",
                    condition="injured", city="Marikina", status="reported",
                    geom=Point(LNG, LAT, srid=4326))
    defaults.update(kw)
    return StrayReport.objects.create(**defaults)


@pytest.mark.django_db
def test_report_detail_does_not_leak_to_a_guest(client):
    reporter = AccountFactory(email="reporter@example.com", display_name="Reporter Rey")
    r = _anon_report(reporter)
    res = client.get(f"/api/v1/reports/{r.pk}")
    assert res.status_code == 200
    _assert_no_leak(res.json(), reporter)


@pytest.mark.django_db
def test_report_detail_does_not_leak_to_an_unrelated_account(client):
    reporter = AccountFactory(email="reporter@example.com", display_name="Reporter Rey")
    r = _anon_report(reporter)
    stranger = AccountFactory()
    res = client.get(f"/api/v1/reports/{r.pk}", **_hdr(stranger))
    _assert_no_leak(res.json(), reporter)


@pytest.mark.django_db
def test_rescue_map_does_not_leak_the_reporter(client):
    reporter = AccountFactory(email="reporter@example.com", display_name="Reporter Rey")
    _anon_report(reporter)
    res = client.get("/api/v1/reports/map", {"city": "Marikina"})
    _assert_no_leak(res.json(), reporter)


@pytest.mark.django_db
def test_offer_creation_response_does_not_leak_the_reporter(client):
    reporter = AccountFactory(email="reporter@example.com", display_name="Reporter Rey")
    r = _anon_report(reporter)
    offerer = AccountFactory()
    res = client.post(f"/api/v1/reports/{r.pk}/offers", {"offer_type": "transport"},
                      **_hdr(offerer))
    assert res.status_code == 201
    _assert_no_leak(res.json(), reporter)


@pytest.mark.django_db
def test_claim_response_does_not_leak_the_reporter(client):
    reporter = AccountFactory(email="reporter@example.com", display_name="Reporter Rey")
    r = _anon_report(reporter)
    claimer = _verified_member()
    res = client.post(f"/api/v1/reports/{r.pk}/claim", **_hdr(claimer))
    assert res.status_code == 201
    _assert_no_leak(res.json(), reporter)


@pytest.mark.django_db
def test_claimers_case_detail_does_not_leak_the_reporter(client):
    reporter = AccountFactory(email="reporter@example.com", display_name="Reporter Rey")
    r = _anon_report(reporter)
    claimer = _verified_member()
    case_id = client.post(f"/api/v1/reports/{r.pk}/claim", **_hdr(claimer)).json()["case_id"]
    res = client.get(f"/api/v1/cases/{case_id}", **_hdr(claimer))
    _assert_no_leak(res.json(), reporter)


@pytest.mark.django_db
def test_claimers_my_rescues_does_not_leak_the_reporter(client):
    reporter = AccountFactory(email="reporter@example.com", display_name="Reporter Rey")
    r = _anon_report(reporter)
    claimer = _verified_member()
    client.post(f"/api/v1/reports/{r.pk}/claim", **_hdr(claimer))
    res = client.get("/api/v1/me/rescues", **_hdr(claimer))
    _assert_no_leak(res.json(), reporter)


@pytest.mark.django_db
def test_offerers_my_offers_does_not_leak_the_reporter(client):
    reporter = AccountFactory(email="reporter@example.com", display_name="Reporter Rey")
    r = _anon_report(reporter)
    offerer = AccountFactory()
    client.post(f"/api/v1/reports/{r.pk}/offers", {"offer_type": "transport"}, **_hdr(offerer))
    res = client.get("/api/v1/me/offers", **_hdr(offerer))
    _assert_no_leak(res.json(), reporter)


@pytest.mark.django_db
def test_claimers_notifications_do_not_leak_the_reporter(client):
    """The claim flow notifies every matched offerer (offer_matched) — that notification's
    body/data must name the report, not who reported it."""
    reporter = AccountFactory(email="reporter@example.com", display_name="Reporter Rey")
    r = _anon_report(reporter)
    offerer = AccountFactory()
    client.post(f"/api/v1/reports/{r.pk}/offers", {"offer_type": "transport"}, **_hdr(offerer))
    claimer = _verified_member()
    client.post(f"/api/v1/reports/{r.pk}/claim", **_hdr(claimer))

    res = client.get("/api/v1/me/notifications", **_hdr(offerer))
    _assert_no_leak(res.json(), reporter)


@pytest.mark.django_db
def test_reopened_cases_notifications_to_prior_offerers_do_not_leak_the_reporter(client):
    """US-E2's case_reopened notification (sagip/sweeps.py) fans out to every prior
    offerer on a stalled claim — same leak surface as offer_matched, different sweep."""
    from sagip.sweeps import expire_stalled_claims

    reporter = AccountFactory(email="reporter@example.com", display_name="Reporter Rey")
    r = _anon_report(reporter, condition="healthy")
    offerer = AccountFactory()
    client.post(f"/api/v1/reports/{r.pk}/offers", {"offer_type": "transport"}, **_hdr(offerer))
    claimer = _verified_member()
    client.post(f"/api/v1/reports/{r.pk}/claim", **_hdr(claimer))

    expire_stalled_claims(now=timezone.now() + timezone.timedelta(hours=25))

    res = client.get("/api/v1/me/notifications", **_hdr(offerer))
    _assert_no_leak(res.json(), reporter)


@pytest.mark.django_db
def test_escalation_notifications_do_not_leak_the_reporter(client):
    """US-E1's report_escalated notification fans out to nearby rescuers — the report
    hasn't even been claimed yet, so there's no relationship to hide behind either."""
    from sagip.sweeps import escalate_reports

    reporter = AccountFactory(email="reporter@example.com", display_name="Reporter Rey")
    r = _anon_report(reporter, condition="healthy")
    StrayReport.objects.filter(pk=r.pk).update(
        created_at=timezone.now() - timezone.timedelta(hours=9))
    rescuer = _verified_member()
    from accounts.models import Address
    Address.objects.create(account=rescuer, city="Marikina", is_primary=True)

    escalate_reports()

    res = client.get("/api/v1/me/notifications", **_hdr(rescuer))
    _assert_no_leak(res.json(), reporter)


@pytest.mark.django_db
def test_the_reporter_still_sees_their_own_report_fully_reporter_is_not_hidden_from_self(client):
    """Anonymity hides the reporter from OTHERS, never from themselves — a sanity check
    that this sweep isn't accidentally asserting the reporter can't see their own data."""
    reporter = AccountFactory(email="reporter@example.com", display_name="Reporter Rey")
    r = _anon_report(reporter)
    res = client.get(f"/api/v1/reports/{r.pk}", **_hdr(reporter))
    assert res.status_code == 200
    assert "status_history" in res.json()  # reporter-only field — proves they're recognized
