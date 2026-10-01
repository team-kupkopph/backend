"""P2 · a removed report is honest to its ex-claimers: anyone with a case on a hidden report who is
no longer its active claimer gets 410 `report_removed` (not a 404 that reads as "never existed"),
My rescues says which rows were removed, strangers and guests still get 404, and a restore undoes it."""
import pytest
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from sagip.moderation import restore_report
from sagip.tests.test_moderation_takedown import _action, _c, _claimed, _report


@pytest.mark.django_db
def test_the_ex_claimer_of_a_taken_down_report_gets_410_report_removed():
    report, case, claimer = _claimed()
    _action(report)
    case.refresh_from_db()
    assert case.expired_at is not None
    res = _c(claimer).get(f"/api/v1/reports/{report.pk}")
    assert res.status_code == 410
    err = res.json()["error"]
    assert err["code"] == "report_removed"
    assert err["message"] == "This report was removed by moderation."


@pytest.mark.django_db
def test_a_stranger_and_a_guest_still_get_404_on_a_removed_report():
    report, _, _ = _claimed()
    _action(report)
    for client in (_c(AccountFactory()), APIClient()):
        res = client.get(f"/api/v1/reports/{report.pk}")
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "not_found"


@pytest.mark.django_db
def test_the_reporter_and_an_active_claimer_keep_their_200():
    report, _, _ = _claimed()
    _action(report)
    mine = _c(report.reporter_account).get(f"/api/v1/reports/{report.pk}")
    assert mine.status_code == 200 and mine.json()["hidden"] is True

    custody, kept_case, keeper = _claimed()
    assert _c(keeper).post(f"/api/v1/cases/{kept_case.pk}/status", {"status": "rescued"},
                           format="json").status_code == 200
    _action(custody)
    kept_case.refresh_from_db()
    assert kept_case.expired_at is None                       # custody left alone: still active
    assert _c(keeper).get(f"/api/v1/reports/{custody.pk}").status_code == 200


@pytest.mark.django_db
def test_my_rescues_flags_a_removed_report_and_not_a_normal_one():
    removed, removed_case, claimer = _claimed()
    _action(removed)
    normal = _report()
    res = _c(claimer).post(f"/api/v1/reports/{normal.pk}/claim")
    assert res.status_code == 201
    normal_case_id = res.json()["case_id"]
    rows = {r["case_id"]: r for r in _c(claimer).get("/api/v1/me/rescues").json()["cases"]}
    assert rows[str(removed_case.pk)]["hidden"] is True
    assert rows[normal_case_id]["hidden"] is False


@pytest.mark.django_db
def test_a_restore_gives_the_ex_claimer_the_public_view_back():
    report, _, claimer = _claimed()
    _action(report)
    assert _c(claimer).get(f"/api/v1/reports/{report.pk}").status_code == 410
    restore_report(report, AccountFactory(), "taken down in error")
    assert _c(claimer).get(f"/api/v1/reports/{report.pk}").status_code == 200
