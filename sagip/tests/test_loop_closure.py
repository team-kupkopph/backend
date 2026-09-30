"""Closing the Sagip loop — the no-decision gaps from dev/sagip-build-review.md.

S8   the claimer gets what they need in the field (the landmark the reporter typed)
S9   the claimer is told the deadline, warned before it, and told when it passes
S10  the reporter hears every step and sees how it ended
S11  the reporter can close a report that no longer needs anyone
S14  the map can place each report (coarsely)
S18  the rescuer hears whether a placement was accepted
S27  a claimer reading the report can find their own case
"""
import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from notifications.models import Notification
from sagip.geo import coarsen_point
from sagip.models import CaseStatusHistory, OfferStatus, ReportOffer, RescueCase, StrayReport
from sagip.sweeps import expire_stalled_claims, warn_due_claims
from verifications.models import AccountCapability

LAT, LNG = 14.6507, 121.1029   # Marikina centre
LANDMARK = "Beside the blue gate, Sumulong Hwy"


def _c(account=None):
    c = APIClient()
    if account is not None:
        c.force_authenticate(user=account)
    return c


def _verified():
    a = AccountFactory()
    AccountCapability.objects.create(account=a, capability="rescuer", status="approved")
    return a


def _report(reporter=None, **kw):
    fields = dict(species="dog", condition="injured", status="reported", city="Marikina",
                  location_text=LANDMARK, geom=Point(LNG, LAT, srid=4326))
    fields.update(kw)
    return StrayReport.objects.create(reporter_account=reporter or AccountFactory(), **fields)


def _claimed(report=None, claimer=None):
    report = report or _report()
    claimer = claimer or _verified()
    res = _c(claimer).post(f"/api/v1/reports/{report.pk}/claim")
    assert res.status_code == 201, res.content
    report.refresh_from_db()
    return report, RescueCase.objects.get(pk=res.json()["case_id"]), claimer


def _detail(report, viewer=None):
    return _c(viewer).get(f"/api/v1/reports/{report.pk}").json()


def _backdate_claim(report, hours):
    CaseStatusHistory.objects.filter(report=report).update(
        changed_at=timezone.now() - timezone.timedelta(hours=hours))


def _notified(account, type_):
    return list(Notification.objects.filter(account=account, type=type_))


# ── S8 · the landmark ────────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_the_landmark_goes_to_the_reporter_and_the_claimer_only():
    """`location_text` is as precise as the pin ("beside the blue gate"), so it follows the
    pin's rule. It used to be written on create and returned by no endpoint at all."""
    report, _, claimer = _claimed()
    assert _detail(report, report.reporter_account)["location_text"] == LANDMARK
    assert _detail(report, claimer)["location_text"] == LANDMARK
    assert "location_text" not in _detail(report, AccountFactory())
    assert "location_text" not in _detail(report)


@pytest.mark.django_db
def test_the_case_detail_carries_the_landmark():
    report, case, claimer = _claimed()
    body = _c(claimer).get(f"/api/v1/cases/{case.pk}").json()
    assert body["report"]["location_text"] == LANDMARK


# ── S9 / S27 · the claimer's deadline, and their way back to the case ────────────────
@pytest.mark.django_db
def test_the_active_claimer_sees_their_case_and_its_deadline():
    report, case, claimer = _claimed()
    claimed_at = CaseStatusHistory.objects.get(report=report).changed_at
    mine = _detail(report, claimer)["my_case"]
    assert mine["case_id"] == str(case.pk)
    assert mine["claim_due_at"] == (claimed_at + timezone.timedelta(hours=6)).isoformat()
    assert "my_case" not in _detail(report, report.reporter_account)
    assert "my_case" not in _detail(report, AccountFactory())


@pytest.mark.django_db
def test_once_the_animal_is_rescued_there_is_no_deadline():
    report, case, claimer = _claimed()
    _c(claimer).post(f"/api/v1/cases/{case.pk}/status", {"status": "rescued"}, format="json")
    assert _detail(report, claimer)["my_case"]["claim_due_at"] is None
    rows = _c(claimer).get("/api/v1/me/rescues").json()["cases"]
    assert rows[0]["claim_due_at"] is None


@pytest.mark.django_db
def test_my_rescues_carries_the_deadline_of_a_claimed_case():
    report, case, claimer = _claimed()
    rows = _c(claimer).get("/api/v1/me/rescues").json()["cases"]
    assert rows[0]["claim_due_at"] is not None


@pytest.mark.django_db
def test_the_claimer_is_warned_once_when_a_quarter_of_the_window_is_left():
    report, case, claimer = _claimed()          # injured: 6h window, warned from 4.5h
    _backdate_claim(report, 4)
    assert warn_due_claims() == []
    _backdate_claim(report, 5)
    assert warn_due_claims() == [case]
    assert warn_due_claims() == []              # the next hourly tick does not repeat it
    [n] = _notified(claimer, "claim_due")
    assert n.data["case_id"] == str(case.pk) and n.data["report_id"] == str(report.pk)


@pytest.mark.django_db
def test_a_rescued_case_is_never_warned():
    report, case, claimer = _claimed()
    _c(claimer).post(f"/api/v1/cases/{case.pk}/status", {"status": "rescued"}, format="json")
    _backdate_claim(report, 5)
    assert warn_due_claims() == []


@pytest.mark.django_db
def test_a_lapsed_claim_tells_the_claimer_and_the_reporter():
    """The confirm dialog said a claim "can't be handed back"; the sweep then took it back
    and told only the offerers."""
    report, case, claimer = _claimed()
    _backdate_claim(report, 7)
    expire_stalled_claims()
    assert len(_notified(claimer, "claim_lapsed")) == 1
    assert len(_notified(report.reporter_account, "case_reopened")) == 1


# ── S21 · a report that isn't open can't be claimed ──────────────────────────────────
@pytest.mark.django_db
@pytest.mark.parametrize("status", ["resolved", "safe"])
def test_only_a_reported_report_can_be_claimed(status):
    """A report resolved without a case (a confirmed lost & found match, or now a reporter
    closing it) had no active case, so the old check let it be claimed back to `claimed`."""
    report = _report(status=status)
    res = _c(_verified()).post(f"/api/v1/reports/{report.pk}/claim")
    assert res.status_code == 409 and res.json()["error"]["code"] == "report_not_open"
    report.refresh_from_db()
    assert report.status == status


# ── S10 · the reporter hears every step and sees the outcome ─────────────────────────
@pytest.mark.django_db
def test_the_reporter_is_told_each_time_the_case_moves():
    report, case, claimer = _claimed()
    for status in ("rescued", "safe"):
        _c(claimer).post(f"/api/v1/cases/{case.pk}/status", {"status": status}, format="json")
    got = [n.data["status"] for n in _notified(report.reporter_account, "case_progress")]
    assert sorted(got) == ["rescued", "safe"]


@pytest.mark.django_db
def test_matched_offerers_hear_the_ending_not_every_step():
    report = _report()
    offerer = AccountFactory()
    ReportOffer.objects.create(report=report, account=offerer, offer_type="transport",
                               expires_at=timezone.now() + timezone.timedelta(hours=48))
    report, case, claimer = _claimed(report)
    _c(claimer).post(f"/api/v1/cases/{case.pk}/status", {"status": "rescued"}, format="json")
    assert _notified(offerer, "case_progress") == []
    _c(claimer).post(f"/api/v1/cases/{case.pk}/status", {"status": "resolved"}, format="json")
    assert [n.data["status"] for n in _notified(offerer, "case_progress")] == ["resolved"]


@pytest.mark.django_db
def test_the_reporter_sees_who_claimed_it_the_steps_and_how_it_ended():
    report, case, claimer = _claimed()
    c = _c(claimer)
    c.post(f"/api/v1/cases/{case.pk}/status", {"status": "rescued", "note": "At the vet"},
           format="json")
    c.post(f"/api/v1/cases/{case.pk}/status",
           {"status": "resolved", "outcome_notes": "Adopted by a neighbour",
            "outcome_photo_url": "s3://outcome.jpg"}, format="json")

    body = _detail(report, report.reporter_account)
    assert body["claimer"] == {"display_name": claimer.display_name}
    assert body["outcome"]["notes"] == "Adopted by a neighbour"
    assert body["outcome"]["photo_url"] == "s3://outcome.jpg"
    assert body["outcome"]["resolved_at"] is not None
    assert [h.get("note") for h in body["status_history"]][:2] == ["", "At the vet"]
    for field in ("claimer", "outcome"):
        assert field not in _detail(report, AccountFactory())


@pytest.mark.django_db
def test_a_lapsed_claimer_is_not_named_to_the_reporter():
    report, case, claimer = _claimed()
    _backdate_claim(report, 7)
    expire_stalled_claims()
    body = _detail(report, report.reporter_account)
    assert body["claimer"] is None and body["outcome"] is None


@pytest.mark.django_db
def test_an_accepted_placement_tells_the_reporter_it_ended():
    report, case, claimer = _claimed()
    _c(claimer).post(f"/api/v1/cases/{case.pk}/status", {"status": "safe"}, format="json")
    recipient = _verified()
    placed = _c(claimer).post(f"/api/v1/cases/{case.pk}/place",
                              {"recipient_email": recipient.email}, format="json").json()
    _c(recipient).post(f"/api/v1/inquiries/{placed['inquiry_id']}/accept")
    got = [n.data["status"] for n in _notified(report.reporter_account, "case_progress")]
    assert "resolved" in got


# ── S11 · the reporter can close a report ────────────────────────────────────────────
@pytest.mark.django_db
def test_the_reporter_can_close_an_unclaimed_report_with_a_reason():
    report = _report()
    offer = ReportOffer.objects.create(report=report, account=AccountFactory(),
                                       offer_type="supplies",
                                       expires_at=timezone.now() + timezone.timedelta(hours=48))
    res = _c(report.reporter_account).post(f"/api/v1/reports/{report.pk}/close",
                                           {"reason": "handled_myself"}, format="json")
    assert res.status_code == 200 and res.json()["status"] == "resolved"

    report.refresh_from_db(); offer.refresh_from_db()
    assert report.status == "resolved"
    assert offer.status == OfferStatus.EXPIRED            # nothing left to offer on
    assert _detail(report, report.reporter_account)["close_reason"] == "handled_myself"
    assert "close_reason" not in _detail(report, AccountFactory())


@pytest.mark.django_db
def test_only_the_reporter_can_close_it():
    report = _report()
    res = _c(AccountFactory()).post(f"/api/v1/reports/{report.pk}/close",
                                    {"reason": "gone"}, format="json")
    assert res.status_code == 403
    report.refresh_from_db()
    assert report.status == "reported"


@pytest.mark.django_db
def test_a_claimed_report_cannot_be_closed_by_the_reporter():
    """Once someone has claimed it, a rescuer is on the way; closing it would strand them.
    The claim either progresses or lapses back to `reported`."""
    report, _, _ = _claimed()
    res = _c(report.reporter_account).post(f"/api/v1/reports/{report.pk}/close",
                                           {"reason": "gone"}, format="json")
    assert res.status_code == 409 and res.json()["error"]["code"] == "report_not_open"


@pytest.mark.django_db
def test_a_close_needs_a_known_reason():
    report = _report()
    res = _c(report.reporter_account).post(f"/api/v1/reports/{report.pk}/close",
                                           {"reason": "bored"}, format="json")
    assert res.status_code in (400, 422)


@pytest.mark.django_db
def test_a_closed_report_is_not_escalated():
    from sagip.sweeps import escalate_reports

    report = _report()
    _c(report.reporter_account).post(f"/api/v1/reports/{report.pk}/close",
                                     {"reason": "gone"}, format="json")
    StrayReport.objects.filter(pk=report.pk).update(
        created_at=timezone.now() - timezone.timedelta(hours=5))
    assert escalate_reports() == []


# ── S14 · the map can place each report ──────────────────────────────────────────────
@pytest.mark.django_db
def test_map_rows_carry_the_coarse_point_and_never_the_precise_one():
    """The same ~500 m grid point GET /reports/{id} already gives anyone — nothing new is
    exposed — so the map can draw each report instead of one circle for the whole city."""
    report = _report()
    [row] = _c().get("/api/v1/reports/map?city=Marikina").json()["reports"]
    lat, lng = coarsen_point(LAT, LNG)
    assert row["approx_location"] == {"lat": lat, "lng": lng}
    assert (lat, lng) != (LAT, LNG)
    assert report.report_id and "precise_location" not in row


# ── S18 · the rescuer hears the placement decision ───────────────────────────────────
@pytest.mark.django_db
@pytest.mark.parametrize("action,decision", [("accept", "accepted"), ("decline", "declined")])
def test_the_rescuer_is_told_whether_the_placement_was_accepted(action, decision):
    report, case, claimer = _claimed()
    _c(claimer).post(f"/api/v1/cases/{case.pk}/status", {"status": "safe"}, format="json")
    recipient = _verified()
    placed = _c(claimer).post(f"/api/v1/cases/{case.pk}/place",
                              {"recipient_email": recipient.email}, format="json").json()
    _c(recipient).post(f"/api/v1/inquiries/{placed['inquiry_id']}/{action}")

    [n] = _notified(claimer, "placement_decided")
    assert n.data["decision"] == decision
    assert n.data["listing_id"] == placed["listing_id"]
