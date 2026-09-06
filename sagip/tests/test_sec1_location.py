"""US-SEC1 — the precise-location split: who gets `precise_location`, who only ever
sees `approx_location`, and a regression sweep confirming no other endpoint leaks the
real point to a caller who shouldn't have it.
"""
import uuid

import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from notifications.models import Notification
from sagip.geo import coarsen_point
from sagip.models import RescueCase, StrayReport
from verifications.models import AccountCapability

LAT, LNG = 14.6507, 121.1029  # Marikina


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _report(**kw):
    defaults = dict(species="dog", condition="injured", city="Marikina", status="reported",
                    geom=Point(LNG, LAT, srid=4326))
    defaults.update(kw)
    return StrayReport.objects.create(**defaults)


def _verified_member():
    acc = AccountFactory()
    AccountCapability.objects.create(account=acc, capability="rescuer", status="approved",
                                     granted_at=timezone.now())
    return acc


def _expected_approx():
    lat, lng = coarsen_point(LAT, LNG)
    return {"lat": lat, "lng": lng}


# ── GET /reports/{id} — approx_location is always there, precise_location is gated ──
@pytest.mark.django_db
def test_approx_location_is_present_for_a_guest(client):
    r = _report()
    body = client.get(f"/api/v1/reports/{r.pk}").json()
    assert body["approx_location"] == _expected_approx()
    assert "precise_location" not in body


@pytest.mark.django_db
def test_precise_location_absent_for_a_signed_in_stranger(client):
    reporter = AccountFactory()
    r = _report(reporter_account=reporter)
    stranger = AccountFactory()
    body = client.get(f"/api/v1/reports/{r.pk}", **_hdr(stranger)).json()
    assert "precise_location" not in body


@pytest.mark.django_db
def test_precise_location_present_for_the_reporter(client):
    reporter = AccountFactory()
    r = _report(reporter_account=reporter)
    body = client.get(f"/api/v1/reports/{r.pk}", **_hdr(reporter)).json()
    assert body["precise_location"] == {"lat": LAT, "lng": LNG}


@pytest.mark.django_db
def test_precise_location_present_for_the_active_claimer(client):
    r = _report(status="claimed")
    claimer = AccountFactory()
    RescueCase.objects.create(report=r, claimed_by_account=claimer)
    body = client.get(f"/api/v1/reports/{r.pk}", **_hdr(claimer)).json()
    assert body["precise_location"] == {"lat": LAT, "lng": LNG}


@pytest.mark.django_db
def test_precise_location_absent_once_the_claim_has_expired(client):
    r = _report(status="reported")
    former_claimer = AccountFactory()
    RescueCase.objects.create(report=r, claimed_by_account=former_claimer,
                              expired_at=timezone.now())
    body = client.get(f"/api/v1/reports/{r.pk}", **_hdr(former_claimer)).json()
    assert "precise_location" not in body


@pytest.mark.django_db
def test_approx_location_never_equals_the_precise_point_when_off_grid(client):
    # Marikina's centroid deliberately isn't grid-aligned, so this specific point should
    # actually move under coarsening — guards against a no-op coarsen function.
    r = _report()
    body = client.get(f"/api/v1/reports/{r.pk}").json()
    assert body["approx_location"] != {"lat": LAT, "lng": LNG}


# ── POST /reports/{id}/claim — the claimer gets precise_location immediately ────────
@pytest.mark.django_db
def test_claiming_returns_the_precise_location(client):
    r = _report()
    member = _verified_member()
    res = client.post(f"/api/v1/reports/{r.pk}/claim", {}, content_type="application/json",
                      **_hdr(member))
    assert res.status_code == 201
    assert res.json()["precise_location"] == {"lat": LAT, "lng": LNG}


# ── GET /cases/{id} — the claimer's own case, precise_location while active ─────────
@pytest.mark.django_db
def test_case_detail_gives_the_claimer_the_precise_spot(client):
    r = _report(status="claimed")
    claimer = AccountFactory()
    case = RescueCase.objects.create(report=r, claimed_by_account=claimer)
    body = client.get(f"/api/v1/cases/{case.pk}", **_hdr(claimer)).json()
    assert body["report"]["precise_location"] == {"lat": LAT, "lng": LNG}
    assert body["report"]["approx_location"] == _expected_approx()


@pytest.mark.django_db
def test_case_detail_refuses_a_non_claimer(client):
    r = _report(status="claimed")
    claimer, someone_else = AccountFactory(), AccountFactory()
    case = RescueCase.objects.create(report=r, claimed_by_account=claimer)
    res = client.get(f"/api/v1/cases/{case.pk}", **_hdr(someone_else))
    assert res.status_code == 403


@pytest.mark.django_db
def test_case_detail_404s_for_an_unknown_case(client):
    res = client.get(f"/api/v1/cases/{uuid.uuid4()}", **_hdr(AccountFactory()))
    assert res.status_code == 404


@pytest.mark.django_db
def test_case_detail_omits_precise_location_once_expired_even_for_the_original_claimer(client):
    r = _report(status="reported")
    claimer = AccountFactory()
    case = RescueCase.objects.create(report=r, claimed_by_account=claimer,
                                     expired_at=timezone.now())
    body = client.get(f"/api/v1/cases/{case.pk}", **_hdr(claimer)).json()
    assert "precise_location" not in body["report"]
    assert body["expired_at"] is not None  # they can still see it lapsed, just not the pin


@pytest.mark.django_db
def test_case_detail_requires_auth(client):
    r = _report(status="claimed")
    case = RescueCase.objects.create(report=r, claimed_by_account=AccountFactory())
    assert client.get(f"/api/v1/cases/{case.pk}").status_code == 401


# ── Regression sweep — nothing else ever echoes the precise point ───────────────────
@pytest.mark.django_db
def test_the_rescue_map_never_carries_a_precise_point(client):
    _report(city="Marikina")
    res = client.get("/api/v1/reports/map?city=Marikina")
    body_str = str(res.json())
    assert "precise_location" not in body_str
    assert str(LAT) not in body_str and str(LNG) not in body_str


@pytest.mark.django_db
def test_my_reports_never_carries_a_precise_point(client):
    reporter = AccountFactory()
    _report(reporter_account=reporter)
    res = client.get("/api/v1/me/reports", **_hdr(reporter))
    body_str = str(res.json())
    assert "precise_location" not in body_str
    assert str(LAT) not in body_str and str(LNG) not in body_str


@pytest.mark.django_db
def test_my_rescues_never_carries_a_precise_point(client):
    claimer = AccountFactory()
    r = _report(status="claimed")
    RescueCase.objects.create(report=r, claimed_by_account=claimer)
    res = client.get("/api/v1/me/rescues", **_hdr(claimer))
    body_str = str(res.json())
    assert "precise_location" not in body_str
    assert str(LAT) not in body_str and str(LNG) not in body_str


@pytest.mark.django_db
def test_my_offers_never_carries_a_precise_point(client):
    from sagip.models import ReportOffer
    offerer = AccountFactory()
    r = _report()
    ReportOffer.objects.create(report=r, account=offerer, offer_type="transport",
                               status="open", expires_at=timezone.now() + timezone.timedelta(hours=48))
    res = client.get("/api/v1/me/offers", **_hdr(offerer))
    body_str = str(res.json())
    assert "precise_location" not in body_str
    assert str(LAT) not in body_str and str(LNG) not in body_str


@pytest.mark.django_db
def test_notification_payloads_never_carry_a_precise_point(client):
    """A claim fan-outs three notification types (offer_matched, report_claimed) — sweep
    every Notification.data dict written during a full claim flow."""
    reporter = AccountFactory()
    r = _report(reporter_account=reporter)
    offerer = AccountFactory()
    from sagip.models import ReportOffer
    ReportOffer.objects.create(report=r, account=offerer, offer_type="transport",
                               status="open", expires_at=timezone.now() + timezone.timedelta(hours=48))
    member = _verified_member()
    client.post(f"/api/v1/reports/{r.pk}/claim", {}, content_type="application/json",
               **_hdr(member))

    for n in Notification.objects.all():
        assert n.data is None or "precise_location" not in n.data
        assert n.data is None or "lat" not in n.data
        assert n.data is None or "lng" not in n.data
