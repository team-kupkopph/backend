"""U1 · staff restore a Sagip report a moderation takedown hid. The takedown (C13) is
deliberate and one-way for the reporter and the public; this is the staff-only way back for a
takedown that was a mistake. It un-hides; it does not undo what the takedown did around it."""
import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from adminapi.models import AdminAuditLog
from adminapi.tests.conftest import full_signin
from notifications.models import Notification
from sagip.models import (
    CaseStatusHistory,
    MatchStatus,
    OfferStatus,
    ReportMatch,
    ReportOffer,
    RescueCase,
    StrayReport,
)
from sagip.moderation import hide_report
from verifications.models import AccountCapability


@pytest.fixture
def auth(client, staffer):
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}


def _c(account):
    c = APIClient(); c.force_authenticate(user=account); return c


def _verified():
    a = AccountFactory()
    AccountCapability.objects.create(account=a, capability="rescuer", status="approved")
    return a


def _report(**kw):
    defaults = dict(reporter_account=AccountFactory(), species="dog", condition="injured",
                    status="reported", city="Marikina", geom=Point(121.10, 14.65, srid=4326))
    defaults.update(kw)
    return StrayReport.objects.create(**defaults)


def _claimed():
    report = _report()
    claimer = _verified()
    res = _c(claimer).post(f"/api/v1/reports/{report.pk}/claim")
    assert res.status_code == 201
    report.refresh_from_db()
    return report, RescueCase.objects.get(pk=res.json()["case_id"]), claimer


def _url(report_id):
    return f"/admin-api/reports/{report_id}/restore"


def _post(client, auth, report_id, body):
    return client.post(_url(report_id), body, content_type="application/json", **auth)


def _on_map(report):
    rows = APIClient().get("/api/v1/reports/map?city=Marikina").json()["reports"]
    return str(report.pk) in [r["report_id"] for r in rows]


@pytest.mark.django_db
def test_restoring_a_hidden_reported_report_puts_it_back_on_the_map_and_in_public_detail(client, auth):
    report = _report()
    hide_report(report, AccountFactory())
    assert not _on_map(report)
    assert APIClient().get(f"/api/v1/reports/{report.pk}").status_code == 404

    res = _post(client, auth, report.pk, {"reason": "Flagged in error; the photo is genuine."})

    assert res.status_code == 200, res.content
    assert res.json() == {"report_id": str(report.pk), "status": "reported", "hidden": False}
    report.refresh_from_db()
    assert report.hidden_at is None
    assert _on_map(report)
    assert APIClient().get(f"/api/v1/reports/{report.pk}").status_code == 200
    # No status move happened, so no history row is invented.
    assert not CaseStatusHistory.objects.filter(report=report).exists()


@pytest.mark.django_db
def test_a_takedown_ended_claim_comes_back_as_reported_and_is_claimable_again(client, auth, staffer):
    report, case, claimer = _claimed()
    hide_report(report, AccountFactory())
    report.refresh_from_db()
    case.refresh_from_db()
    assert report.status == "claimed" and case.expired_at is not None

    res = _post(client, auth, report.pk, {"reason": "Wrongly actioned"})

    assert res.status_code == 200, res.content
    assert res.json()["status"] == "reported"
    report.refresh_from_db()
    assert report.hidden_at is None and report.status == "reported"
    row = CaseStatusHistory.objects.filter(report=report, status="reported").get()
    assert row.note == "restored_by_moderation:Wrongly actioned"
    assert row.changed_by_account_id == staffer.admin_account.account_id
    # The ended case stays ended: history, not a claim to resurrect.
    case.refresh_from_db()
    assert case.expired_at is not None
    assert _on_map(report)
    assert _c(_verified()).post(f"/api/v1/reports/{report.pk}/claim").status_code == 201


@pytest.mark.django_db
def test_a_long_reason_is_clipped_into_the_history_note(client, auth):
    report, _, _ = _claimed()
    hide_report(report, AccountFactory())
    _post(client, auth, report.pk, {"reason": "x" * 400})
    note = CaseStatusHistory.objects.get(report=report, status="reported").note
    assert note == "restored_by_moderation:" + "x" * 150


@pytest.mark.django_db
def test_a_report_in_custody_stays_rescued_with_its_case_untouched(client, auth):
    report, case, _ = _claimed()
    StrayReport.objects.filter(pk=report.pk).update(status="rescued")
    report.refresh_from_db()
    hide_report(report, AccountFactory())
    case.refresh_from_db()
    assert case.expired_at is None    # an animal in someone's care keeps its case

    res = _post(client, auth, report.pk, {"reason": "Restore"})

    assert res.status_code == 200, res.content
    report.refresh_from_db()
    case.refresh_from_db()
    assert report.hidden_at is None and report.status == "rescued"
    assert case.expired_at is None
    assert not CaseStatusHistory.objects.filter(report=report,
                                                note__startswith="restored_by_moderation").exists()


@pytest.mark.django_db
def test_a_restore_does_not_resurrect_offers_or_matches_or_notify_anyone(client, auth):
    report, _, claimer = _claimed()
    offer = ReportOffer.objects.create(
        report=report, account=AccountFactory(), offer_type="transport", status=OfferStatus.OPEN,
        expires_at=timezone.now() + timezone.timedelta(hours=40))
    other = _report()
    match = ReportMatch.objects.create(report=report, matched_report=other,
                                       status=MatchStatus.SUGGESTED, score=0.9)
    hide_report(report, AccountFactory())
    before = Notification.objects.count()

    assert _post(client, auth, report.pk, {"reason": "Restore"}).status_code == 200

    offer.refresh_from_db()
    match.refresh_from_db()
    assert offer.status == OfferStatus.EXPIRED
    assert match.status == MatchStatus.DISMISSED
    assert Notification.objects.count() == before


@pytest.mark.django_db
def test_restoring_a_report_that_is_not_hidden_is_409(client, auth):
    report = _report()
    res = _post(client, auth, report.pk, {"reason": "Restore"})
    assert res.status_code == 409 and res.json()["error"]["code"] == "not_hidden"
    assert res.json()["error"]["message"]


@pytest.mark.django_db
@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "   "}, {"reason": None},
                                  {"reason": 5}, ["reason"]])
def test_a_restore_without_a_reason_is_422_and_changes_nothing(client, auth, body):
    report = _report()
    hide_report(report, AccountFactory())
    res = _post(client, auth, report.pk, body)
    assert res.status_code == 422 and res.json()["error"]["code"] == "reason_required"
    assert res.json()["error"]["message"]
    report.refresh_from_db()
    assert report.hidden_at is not None


@pytest.mark.django_db
def test_an_unknown_report_is_404(client, auth):
    res = _post(client, auth, "7d1d3b5e-0000-4000-8000-000000000000", {"reason": "Restore"})
    assert res.status_code == 404 and res.json()["error"]["code"] == "not_found"


@pytest.mark.django_db
def test_a_restore_needs_a_staff_token(client):
    report = _report()
    hide_report(report, AccountFactory())
    assert client.post(_url(report.pk), {"reason": "x"},
                       content_type="application/json").status_code in (401, 403)
    # A mobile account token is not a staff token either.
    res = _c(AccountFactory()).post(_url(report.pk), {"reason": "x"}, format="json")
    assert res.status_code in (401, 403)
    report.refresh_from_db()
    assert report.hidden_at is not None


@pytest.mark.django_db
def test_a_restore_is_audited_with_the_report_and_the_reason_length(client, auth, staffer):
    report = _report()
    hide_report(report, AccountFactory())
    AdminAuditLog.objects.all().delete()

    _post(client, auth, report.pk, {"reason": "Flagged in error"})

    row = AdminAuditLog.objects.get()
    assert row.actor_id == staffer.admin_account.account_id
    assert row.action == "reports/{id}/restore" and row.outcome == "ok"
    assert str(row.target_id) == str(report.pk)
    assert row.detail["notes_len"] == len("Flagged in error")
    assert "error" not in str(row.detail)    # the length, never the text
