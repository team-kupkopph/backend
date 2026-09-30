"""S2 / S20 (dev/sagip-build-review.md) · a handoff is the end of a rescue, and happens once.

S2: accepting a direct placement of a rescued animal used to leave its report at `safe` with
the case open, so the rescuer's Home kept saying "Find them a home" for an animal that had
one. S20: nothing stopped the same case being listed AND placed, or placed twice.
"""
import pytest
from django.contrib.gis.geos import Point
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from listings.models import AdoptionListing
from sagip.models import CaseStatusHistory, RescueCase, StrayReport, StrayStatus
from verifications.models import AccountCapability


def _c(a):
    c = APIClient(); c.force_authenticate(user=a); return c


def _verified(a):
    AccountCapability.objects.create(account=a, capability="rescuer", status="approved")
    return a


def _safe_case(rescuer):
    report = StrayReport.objects.create(species="dog", condition="injured", city="Marikina",
        status=StrayStatus.SAFE, geom=Point(121.05, 14.63, srid=4326))
    return RescueCase.objects.create(report=report, claimed_by_account=rescuer)


def _place(rescuer, case, recipient):
    return _c(rescuer).post(f"/api/v1/cases/{case.pk}/place",
        {"recipient_email": recipient.email, "name": "Bruno", "adoption_fee": "0"}, format="json")


def _list(rescuer, case):
    return _c(rescuer).post(f"/api/v1/cases/{case.pk}/list",
        {"name": "Bruno", "adoption_fee": "0"}, format="json")


# ── S2 · the accept closes the rescue ────────────────────────────────────────────────
@pytest.mark.django_db
def test_accepting_a_placement_resolves_the_report_and_closes_the_case():
    rescuer = AccountFactory(); recipient = _verified(AccountFactory())
    case = _safe_case(rescuer)
    inquiry_id = _place(rescuer, case, recipient).json()["inquiry_id"]

    assert _c(recipient).post(f"/api/v1/inquiries/{inquiry_id}/accept").status_code == 200

    case.refresh_from_db(); case.report.refresh_from_db()
    assert case.report.status == StrayStatus.RESOLVED
    assert case.resolved_at is not None
    last = CaseStatusHistory.objects.filter(report=case.report).latest("changed_at")
    assert last.status == StrayStatus.RESOLVED and last.changed_by_account_id == recipient.pk


@pytest.mark.django_db
def test_after_the_accept_the_claimer_has_nothing_left_to_move():
    rescuer = AccountFactory(); recipient = _verified(AccountFactory())
    case = _safe_case(rescuer)
    inquiry_id = _place(rescuer, case, recipient).json()["inquiry_id"]
    _c(recipient).post(f"/api/v1/inquiries/{inquiry_id}/accept")

    res = _c(rescuer).post(f"/api/v1/cases/{case.pk}/status", {"status": "resolved"}, format="json")
    assert res.status_code == 409 and res.json()["error"]["code"] == "case_resolved"


@pytest.mark.django_db
def test_declining_a_placement_leaves_the_rescue_open_and_does_not_publish_it():
    # The animal is still with the rescuer, so the case stays `safe`. The placement listing
    # was never public; a decline must not make it public (it used to flip to `available`).
    rescuer = AccountFactory(); recipient = _verified(AccountFactory())
    case = _safe_case(rescuer)
    body = _place(rescuer, case, recipient).json()

    assert _c(recipient).post(f"/api/v1/inquiries/{body['inquiry_id']}/decline").status_code == 200

    case.refresh_from_db(); case.report.refresh_from_db()
    assert case.report.status == StrayStatus.SAFE and case.resolved_at is None
    assert AdoptionListing.objects.get(pk=body["listing_id"]).status == "withdrawn"


# ── S20 · one live handoff per rescue ────────────────────────────────────────────────
@pytest.mark.django_db
@pytest.mark.parametrize("first,second", [
    ("place", "place"), ("place", "list"), ("list", "place"), ("list", "list"),
])
def test_a_second_handoff_while_one_is_live_is_refused(first, second):
    rescuer = AccountFactory(); recipient = _verified(AccountFactory())
    case = _safe_case(rescuer)
    do = {"place": lambda: _place(rescuer, case, recipient), "list": lambda: _list(rescuer, case)}

    assert do[first]().status_code == 201
    res = do[second]()
    assert res.status_code == 409 and res.json()["error"]["code"] == "already_handed_off"
    assert AdoptionListing.objects.filter(source_report=case.report).count() == 1


@pytest.mark.django_db
def test_after_a_decline_the_rescuer_can_place_with_someone_else():
    rescuer = AccountFactory()
    first, second = _verified(AccountFactory()), _verified(AccountFactory())
    case = _safe_case(rescuer)
    _c(first).post(f"/api/v1/inquiries/{_place(rescuer, case, first).json()['inquiry_id']}/decline")

    assert _place(rescuer, case, second).status_code == 201


@pytest.mark.django_db(transaction=True)
def test_two_places_at_once_produce_one_handoff():
    """⚠️ The lock, PROVED WITH THREADS. A double tap on "Place" is two requests that both
    pass the gate unless the case row is locked; each would create a listing and an offer
    to the recipient. With the lock the second waits, then sees the first's listing."""
    import threading

    from django.db import connection

    rescuer = AccountFactory(); recipient = _verified(AccountFactory())
    case = _safe_case(rescuer)
    results, lock = [], threading.Lock()

    def place():
        try:
            res = _place(rescuer, case, recipient)
            with lock:
                results.append(res.status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=place) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [201, 409], f"expected exactly one winner, got {results}"
    assert AdoptionListing.objects.filter(source_report=case.report).count() == 1
