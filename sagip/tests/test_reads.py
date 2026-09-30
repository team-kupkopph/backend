"""US-S3 (GET /me/reports), US-S5 + US-O3 (GET /reports/{id})."""
import uuid

import pytest
from django.contrib.gis.geos import Point

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from sagip.models import ReportOffer, StrayReport, StrayReportPhoto
from sagip.status import set_report_status


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _report(reporter=None, **kw):
    kw.setdefault("species", "dog")
    kw.setdefault("condition", "injured")
    return StrayReport.objects.create(reporter_account=reporter,
                                      geom=Point(121.05, 14.63, srid=4326), **kw)


# ── US-S3 · my reports ────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_my_reports_requires_auth(client):
    assert client.get("/api/v1/me/reports").status_code == 401


@pytest.mark.django_db
def test_my_reports_lists_only_the_callers_reports_with_status(client):
    me, other = AccountFactory(), AccountFactory()
    mine = _report(me, city="Marikina", status="reported")
    _report(other)
    body = client.get("/api/v1/me/reports", **_hdr(me)).json()["results"]
    assert [r["report_id"] for r in body] == [str(mine.report_id)]
    assert set(body[0].keys()) == {"report_id", "species", "condition", "status", "city", "created_at"}
    assert body[0]["status"] == "reported"


# ── US-S5 · report detail (public, shared-link) ───────────────────────────────────
@pytest.mark.django_db
def test_report_detail_is_public_and_city_level(client):
    r = _report(species="cat", condition="sick", notes="Limping badly.",
                city="Pasig", status="reported")
    StrayReportPhoto.objects.create(report=r, url="https://example.invalid/1")
    body = client.get(f"/api/v1/reports/{r.report_id}").json()
    assert body["report_id"] == str(r.report_id)
    assert body["species"] == "cat" and body["condition"] == "sick"
    assert body["notes"] == "Limping badly." and body["status"] == "reported"
    assert body["city"] == "Pasig"
    assert body["photos"] == ["https://example.invalid/1"]
    # §12.5 — a shared link never exposes the exact spot
    assert "lat" not in body and "lng" not in body and "geom" not in body


@pytest.mark.django_db
def test_report_detail_404_for_unknown_id(client):
    assert client.get(f"/api/v1/reports/{uuid.uuid4()}").status_code == 404


# ── US-O3 · the reporter's waiting view ────────────────────────────────────────────
@pytest.mark.django_db
def test_an_anonymous_caller_never_sees_the_reporter_only_fields(client):
    reporter = AccountFactory()
    r = _report(reporter, city="Pasig", status="reported")
    body = client.get(f"/api/v1/reports/{r.report_id}").json()
    assert "escalation_level" not in body
    assert "offers_count" not in body
    assert "status_history" not in body
    assert "escalation_notified" not in body


@pytest.mark.django_db
def test_a_signed_in_non_reporter_never_sees_the_reporter_only_fields(client):
    """One route, two field-authorization tiers — a valid bearer token that isn't the
    reporter's must not unlock the extra block (the exact leak this endpoint's shared-
    route shape makes possible if the check is skipped)."""
    reporter, someone_else = AccountFactory(), AccountFactory()
    r = _report(reporter, city="Pasig", status="reported")
    body = client.get(f"/api/v1/reports/{r.report_id}", **_hdr(someone_else)).json()
    assert "escalation_level" not in body
    assert "offers_count" not in body
    assert "status_history" not in body
    assert "escalation_notified" not in body


@pytest.mark.django_db
def test_the_reporter_sees_escalation_offers_count_and_status_history(client):
    reporter = AccountFactory()
    r = _report(reporter, city="Pasig", status="reported")
    r.escalation_level = 1
    r.save(update_fields=["escalation_level"])
    ReportOffer.objects.create(report=r, account=AccountFactory(), offer_type="transport",
                               status="open", expires_at=r.created_at)
    ReportOffer.objects.create(report=r, account=AccountFactory(), offer_type="supplies",
                               status="open", expires_at=r.created_at)
    set_report_status(r, "claimed", AccountFactory())

    body = client.get(f"/api/v1/reports/{r.report_id}", **_hdr(reporter)).json()
    assert body["escalation_level"] == 1
    assert body["offers_count"] == 2
    assert [h["status"] for h in body["status_history"]] == ["claimed"]


@pytest.mark.django_db
def test_the_reporter_still_gets_no_precise_geom_in_their_own_view(client):
    reporter = AccountFactory()
    r = _report(reporter, city="Pasig", status="reported")
    body = client.get(f"/api/v1/reports/{r.report_id}", **_hdr(reporter)).json()
    assert "lat" not in body and "lng" not in body and "geom" not in body


# S5 · the reporter's waiting view said "partner shelters notified" whenever the level was 2 —
# including when nobody was, because no partner could exist. It now carries how many people
# each level actually reached, counted from the notification rows the sweep wrote, so the
# client can say "3 rescuers nearby were alerted" or "no one could be reached yet".
@pytest.mark.django_db
def test_the_reporter_sees_how_many_people_each_escalation_level_reached(client):
    from django.utils import timezone

    from accounts.models import Address
    from sagip.sweeps import escalate_reports
    from verifications.models import AccountCapability

    reporter = AccountFactory()
    r = _report(reporter, city="Marikina", status="reported")
    for _ in range(2):
        rescuer = AccountFactory()
        AccountCapability.objects.create(account=rescuer, capability="rescuer", status="approved")
        Address.objects.create(account=rescuer, city="Marikina City", is_primary=True)
    _report(AccountFactory(), city="Marikina", status="reported")   # another report: not counted

    escalate_reports(now=timezone.now() + timezone.timedelta(hours=5))   # injured: past both

    body = client.get(f"/api/v1/reports/{r.report_id}", **_hdr(reporter)).json()
    assert body["escalation_level"] == 2
    # at_report is 0, not None: an injured stray is one the report-time alert covers (D2), it
    # just wasn't filed through the API here, so no one was paged at report time.
    assert body["escalation_notified"] == {"level_1": 2, "level_2": 0, "at_report": 0}


@pytest.mark.django_db
def test_an_unescalated_report_has_reached_no_one_yet(client):
    reporter = AccountFactory()
    r = _report(reporter, city="Marikina", status="reported")
    body = client.get(f"/api/v1/reports/{r.report_id}", **_hdr(reporter)).json()
    assert body["escalation_notified"] == {"level_1": 0, "level_2": 0, "at_report": 0}
