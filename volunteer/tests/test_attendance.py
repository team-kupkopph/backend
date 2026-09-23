import threading

import pytest
from django.db import connection
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from volunteer.models import SignupStatus, VolunteerShift, VolunteerSignup


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _signup(hours_out=-3, status=SignupStatus.APPROVED):
    """Negative hours_out = the shift already happened."""
    shift = VolunteerShift.objects.create(
        shelter_account=AccountFactory(account_type="shelter"),
        starts_at=timezone.now() + timezone.timedelta(hours=hours_out),
        ends_at=timezone.now() + timezone.timedelta(hours=hours_out + 2), capacity=2)
    su = VolunteerSignup.objects.create(shift=shift, volunteer_account=AccountFactory(),
                                        status=status, waiver_accepted=True)
    return shift, su


@pytest.mark.django_db
def test_check_in_then_out(client):
    shift, su = _signup(hours_out=0)
    assert client.post(f"/api/v1/signups/{su.pk}/check-in",
                       **_hdr(su.volunteer_account)).status_code == 200
    assert client.post(f"/api/v1/signups/{su.pk}/check-out",
                       **_hdr(su.volunteer_account)).status_code == 200
    su.refresh_from_db()
    assert su.check_in_at is not None and su.check_out_at is not None


@pytest.mark.django_db
def test_only_an_approved_signup_can_check_in(client):
    shift, su = _signup(status=SignupStatus.REQUESTED)
    res = client.post(f"/api/v1/signups/{su.pk}/check-in", **_hdr(su.volunteer_account))
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "not_approved"


@pytest.mark.django_db
def test_shelter_marks_attended(client):
    shift, su = _signup()
    res = client.post(f"/api/v1/shelter/signups/{su.pk}/attendance",
                      {"outcome": "completed"}, content_type="application/json",
                      **_hdr(shift.shelter_account))
    assert res.status_code == 200
    su.refresh_from_db()
    assert su.status == SignupStatus.COMPLETED


@pytest.mark.django_db
def test_shelter_marks_no_show(client):
    shift, su = _signup()
    client.post(f"/api/v1/shelter/signups/{su.pk}/attendance", {"outcome": "no_show"},
                content_type="application/json", **_hdr(shift.shelter_account))
    su.refresh_from_db()
    assert su.status == SignupStatus.NO_SHOW


@pytest.mark.django_db
def test_attendance_cannot_be_marked_before_the_shift_ends(client):
    shift, su = _signup(hours_out=+5)      # still in the future
    res = client.post(f"/api/v1/shelter/signups/{su.pk}/attendance",
                      {"outcome": "completed"}, content_type="application/json",
                      **_hdr(shift.shelter_account))
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "shift_not_ended"


@pytest.mark.django_db
def test_another_shelter_cannot_mark_attendance(client):
    shift, su = _signup()
    res = client.post(f"/api/v1/shelter/signups/{su.pk}/attendance",
                      {"outcome": "completed"}, content_type="application/json",
                      **_hdr(AccountFactory(account_type="shelter")))
    assert res.status_code == 403


@pytest.mark.django_db
def test_a_non_owner_cannot_check_in_to_someone_elses_signup(client):
    """Parked-4 · the check-in/out owner-403 path. An authenticated volunteer who is not
    the owner of the signup must be refused with the domain-specific `not_your_signup`
    code, not silently allowed to check into another person's shift."""
    shift, su = _signup()
    res = client.post(f"/api/v1/signups/{su.pk}/check-in", **_hdr(AccountFactory()))
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "not_your_signup"


@pytest.mark.django_db(transaction=True)
def test_attendance_write_racing_a_late_cancel_cannot_corrupt_the_terminal_state(client):
    """I-1 · the reason the attendance lock exists. A volunteer may cancel an `approved`
    signup after the shift has started (cancel gates on status, not time), so the shelter
    marking attendance and the volunteer cancelling the SAME row can race. Without
    select_for_update + a re-check under the lock, both pass their `status == APPROVED`
    check and the last writer wins — a `cancelled` overwritten to `no_show`, or a
    `completed` clobbered back to `cancelled`. Exactly one side must win: one 200, one 409,
    and the stored terminal status must match the winner.
    """
    for _ in range(3):
        shift, su = _signup()          # shift already ended, signup APPROVED
        shelter_hdr = _hdr(shift.shelter_account)
        volunteer_hdr = _hdr(su.volunteer_account)
        results = {}
        barrier = threading.Barrier(2)

        def mark_attendance():
            try:
                barrier.wait(timeout=5)
                r = client.post(f"/api/v1/shelter/signups/{su.pk}/attendance",
                                {"outcome": "completed"}, content_type="application/json",
                                **shelter_hdr)
                results["attendance"] = r.status_code
            finally:
                connection.close()  # each thread holds its own DB connection

        def late_cancel():
            try:
                barrier.wait(timeout=5)
                r = client.post(f"/api/v1/signups/{su.pk}/cancel", **volunteer_hdr)
                results["cancel"] = r.status_code
            finally:
                connection.close()

        threads = [threading.Thread(target=mark_attendance),
                   threading.Thread(target=late_cancel)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert sorted(results.values()) == [200, 409], \
            f"expected one win and one 409, got {results}"

        su.refresh_from_db()
        if results["attendance"] == 200:
            assert results["cancel"] == 409
            assert su.status == SignupStatus.COMPLETED
        else:
            assert results["cancel"] == 200
            assert su.status == SignupStatus.CANCELLED
