"""IB1 / IQ1 · an undone attendance takes back what it earned, silently (owner decision 2026-11-05).

A shelter's 24 h undo left "First Kawang-Gawa" beside `shifts_completed: 0` and an unread
"New badge!" — the one thing US-B2 forbids. Only the shift family reconciles: D13's "badges
already awarded are kept" still covers a rescue badge, and so a Bayani whose rescue leg has
since stopped counting survives an undo that leaves the shift leg standing.
"""
from unittest import mock

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from community.badges import award_badges_for
from community.models import AccountBadge, Badge
from listings.tests.adoption_helpers import c
from notifications.models import Notification
from notifications.service import notify
from volunteer.models import SignupStatus, VolunteerShift, VolunteerSignup


def _signup(volunteer, shelter_account, status=SignupStatus.COMPLETED):
    now = timezone.now()
    shift = VolunteerShift.objects.create(shelter_account=shelter_account,
                                          starts_at=now - timezone.timedelta(hours=3),
                                          ends_at=now - timezone.timedelta(hours=1))
    return VolunteerSignup.objects.create(shift=shift, volunteer_account=volunteer, status=status,
                                          waiver_accepted=True, attendance_marked_at=now)


def _volunteer_with(shifts):
    """(volunteer, shelter, the last signup), with `shifts` completed and their badges awarded."""
    volunteer, shelter_account = AccountFactory(), AccountFactory(account_type="shelter")
    signups = [_signup(volunteer, shelter_account) for _ in range(shifts)]
    award_badges_for(volunteer)
    return volunteer, shelter_account, signups[-1]


def _undo(shelter_account, signup):
    return c(shelter_account).post(f"/api/v1/shelter/signups/{signup.pk}/attendance/undo")


def _held(account):
    return set(AccountBadge.objects.filter(account=account).values_list("badge_id", flat=True))


def _notices(account):
    return Notification.objects.filter(account=account, type="badge_earned")


@pytest.mark.django_db
def test_undoing_the_only_shift_takes_the_badge_back():
    volunteer, shelter_account, signup = _volunteer_with(1)
    assert _undo(shelter_account, signup).status_code == 200
    assert _held(volunteer) == set()


@pytest.mark.django_db
def test_the_impact_screen_agrees_after_an_undo():
    volunteer, shelter_account, signup = _volunteer_with(1)
    _undo(shelter_account, signup)
    body = c(volunteer).get("/api/v1/me/impact").json()
    assert body["impact"]["shifts_completed"] == 0
    assert not {b["badge_code"]: b["earned"] for b in body["badges"]}["first_shift"]


@pytest.mark.django_db
def test_the_new_badge_notice_goes_with_it():
    volunteer, shelter_account, signup = _volunteer_with(1)
    assert _notices(volunteer).count() == 1
    _undo(shelter_account, signup)
    assert _notices(volunteer).count() == 0


@pytest.mark.django_db
def test_a_read_notice_goes_too():
    """Silent means nothing is left to explain, read or not."""
    volunteer, shelter_account, signup = _volunteer_with(1)
    _notices(volunteer).update(read=True)
    _undo(shelter_account, signup)
    assert _notices(volunteer).count() == 0


@pytest.mark.django_db
def test_the_revoke_is_one_event_with_its_reason(django_capture_on_commit_callbacks):
    volunteer, shelter_account, signup = _volunteer_with(1)
    with mock.patch("community.badges.emit") as emit, \
            django_capture_on_commit_callbacks(execute=True):
        _undo(shelter_account, signup)
    emit.assert_called_once_with("badge_revoked", badge_code="first_shift", reason="attendance_undo")


@pytest.mark.django_db
def test_ten_down_to_nine_drops_only_the_ten_badge():
    volunteer, shelter_account, signup = _volunteer_with(10)
    _undo(shelter_account, signup)
    assert _held(volunteer) == {"first_shift"}
    assert list(_notices(volunteer).values_list("data__badge_code", flat=True)) == ["first_shift"]


@pytest.mark.django_db
def test_eleven_down_to_ten_keeps_every_badge():
    volunteer, shelter_account, signup = _volunteer_with(11)
    _undo(shelter_account, signup)
    assert _held(volunteer) == {"first_shift", "shifts_10"}
    assert _notices(volunteer).count() == 2


@pytest.mark.django_db
def test_undoing_a_no_show_touches_no_badge():
    volunteer, shelter_account, _ = _volunteer_with(1)
    no_show = _signup(volunteer, shelter_account, SignupStatus.NO_SHOW)
    assert _undo(shelter_account, no_show).status_code == 200
    assert _held(volunteer) == {"first_shift"} and _notices(volunteer).count() == 1


@pytest.mark.django_db
def test_a_kept_rescue_badge_survives_an_undo():
    """D13 · `first_rescue` held from before self-rescues stopped counting: not ours to judge here."""
    volunteer, shelter_account, signup = _volunteer_with(1)
    AccountBadge.objects.create(account=volunteer, badge=Badge.objects.get(pk="first_rescue"))
    _undo(shelter_account, signup)
    assert _held(volunteer) == {"first_rescue"}


@pytest.mark.django_db
def test_bayani_survives_while_a_shift_still_stands():
    """Its rescue and rehome legs read zero here, as a D13-kept Bayani's would: the undo left the
    shift leg standing, so it must not be re-judged on the other two."""
    volunteer, shelter_account, signup = _volunteer_with(2)
    AccountBadge.objects.create(account=volunteer, badge=Badge.objects.get(pk="bayani"))
    _undo(shelter_account, signup)
    assert _held(volunteer) == {"first_shift", "bayani"}


@pytest.mark.django_db
def test_bayani_goes_when_the_last_shift_goes():
    volunteer, shelter_account, signup = _volunteer_with(1)
    AccountBadge.objects.create(account=volunteer, badge=Badge.objects.get(pk="bayani"))
    _undo(shelter_account, signup)
    assert _held(volunteer) == set()


@pytest.mark.django_db
def test_another_volunteers_badge_is_left_alone():
    other, _, _ = _volunteer_with(1)
    volunteer, shelter_account, signup = _volunteer_with(1)
    _undo(shelter_account, signup)
    assert _held(other) == {"first_shift"} and _notices(other).count() == 1


@pytest.mark.django_db
def test_another_notice_type_is_left_alone():
    volunteer, shelter_account, signup = _volunteer_with(1)
    notify(volunteer, "shift_confirmed", title="x", data={"badge_code": "first_shift"})
    _undo(shelter_account, signup)
    assert Notification.objects.filter(account=volunteer, type="shift_confirmed").count() == 1


@pytest.mark.django_db
def test_a_failed_revoke_undoes_the_undo():
    """The badge must not outlive its shift, so the shelter gets a 500 it can retry."""
    volunteer, shelter_account, signup = _volunteer_with(1)
    client = c(shelter_account)
    client.raise_request_exception = False
    with mock.patch("community.badges.reconcile_shift_badges", side_effect=RuntimeError("boom")):
        res = client.post(f"/api/v1/shelter/signups/{signup.pk}/attendance/undo")
    signup.refresh_from_db()
    assert res.status_code == 500 and signup.status == SignupStatus.COMPLETED
    assert _held(volunteer) == {"first_shift"}


@pytest.mark.django_db
def test_mark_undo_mark_awards_once_more(django_capture_on_commit_callbacks):
    volunteer, shelter_account, signup = _volunteer_with(1)
    _undo(shelter_account, signup)
    with django_capture_on_commit_callbacks(execute=True):
        res = c(shelter_account).post(f"/api/v1/shelter/signups/{signup.pk}/attendance",
                                      {"outcome": "completed"}, format="json")
    assert res.status_code == 200
    assert _held(volunteer) == {"first_shift"} and _notices(volunteer).count() == 1
