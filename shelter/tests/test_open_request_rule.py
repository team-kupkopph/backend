"""SH4 (shelter home review, 2026-10-29) · one meaning of "open" for a volunteer request.

Home's `pending_requests` counted `requested` signups whose shift had NOT ended; the inbox's
`status=open` listed every `requested` signup, so a request for a finished shift stayed amber
"Requested" forever (the probe: Home said 1, the inbox listed 2). Both now read
`VolunteerSignup.objects.awaiting_decision(shelter)`.

SQ1 (decided 2026-11-04): an ended-but-undecided request is swept to `declined` with
`end_reason = shift_passed`, and the volunteer gets the existing `signup_declined` notice once.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from notifications.models import Notification
from sagip.management.commands.run_sweeps import SWEEPS
from volunteer.models import ShiftStatus, SignupEndReason, SignupStatus, VolunteerShift, VolunteerSignup
from volunteer.sweeps import decline_lapsed_requests

INBOX = "/api/v1/shelter/requests"
DASH = "/api/v1/shelter/dashboard"


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _shift(shelter, ends_at, **kw):
    return VolunteerShift.objects.create(
        shelter_account=shelter, type="walking", starts_at=ends_at - timedelta(hours=2),
        ends_at=ends_at, capacity=5, status=ShiftStatus.OPEN, **kw)


def _signup(shift, status=SignupStatus.REQUESTED):
    return VolunteerSignup.objects.create(
        shift=shift, volunteer_account=AccountFactory(account_type="personal"), status=status)


@pytest.fixture
def shelter():
    return AccountFactory(account_type="shelter", email_verified_at=timezone.now())


@pytest.mark.django_db
class TestAwaitingDecision:
    def test_a_shift_ending_exactly_now_is_not_awaiting(self, shelter):
        now = timezone.now()
        _signup(_shift(shelter, ends_at=now))
        assert VolunteerSignup.objects.awaiting_decision(shelter, now=now).count() == 0

    def test_a_shift_ending_in_one_second_is_awaiting(self, shelter):
        now = timezone.now()
        su = _signup(_shift(shelter, ends_at=now + timedelta(seconds=1)))
        assert list(VolunteerSignup.objects.awaiting_decision(shelter, now=now)) == [su]

    def test_only_requested_rows_on_this_shelters_shifts(self, shelter):
        later = timezone.now() + timedelta(days=1)
        shift = _shift(shelter, ends_at=later)
        mine = _signup(shift)
        _signup(shift, status=SignupStatus.APPROVED)
        _signup(shift, status=SignupStatus.DECLINED)
        other = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
        _signup(_shift(other, ends_at=later))
        assert list(VolunteerSignup.objects.awaiting_decision(shelter)) == [mine]


@pytest.mark.django_db
class TestHomeAndInboxAgree:
    def test_home_count_equals_inbox_rows_with_a_past_shift_request_present(self, client, shelter):
        now = timezone.now()
        _signup(_shift(shelter, ends_at=now + timedelta(days=1)))
        _signup(_shift(shelter, ends_at=now - timedelta(hours=1)))    # the ghost request

        home = client.get(DASH, **_hdr(shelter)).json()["volunteer"]["pending_requests"]
        inbox = client.get(INBOX, {"kind": "volunteer", "status": "open"}, **_hdr(shelter)).json()["results"]
        assert home == 1
        assert len(inbox) == home

    def test_the_unfiltered_inbox_still_lists_the_past_request(self, client, shelter):
        # Only "open" changes meaning; history is not hidden.
        _signup(_shift(shelter, ends_at=timezone.now() - timedelta(hours=1)))
        rows = client.get(INBOX, {"kind": "volunteer"}, **_hdr(shelter)).json()["results"]
        assert [r["status"] for r in rows] == ["requested"]


@pytest.mark.django_db
class TestDeclineLapsedRequests:
    def test_sweeps_an_ended_request_to_declined_with_a_reason_and_one_notice(self, shelter):
        now = timezone.now()
        lapsed = _signup(_shift(shelter, ends_at=now - timedelta(minutes=5)))

        swept = decline_lapsed_requests(now=now)

        lapsed.refresh_from_db()
        assert swept == [lapsed]
        assert lapsed.status == SignupStatus.DECLINED
        assert lapsed.end_reason == SignupEndReason.SHIFT_PASSED == "shift_passed"
        notes = Notification.objects.filter(account=lapsed.volunteer_account, type="signup_declined")
        assert notes.count() == 1
        assert notes.get().data == {"shift_id": str(lapsed.shift_id), "signup_id": str(lapsed.pk)}

    def test_is_idempotent(self, shelter):
        now = timezone.now()
        lapsed = _signup(_shift(shelter, ends_at=now - timedelta(minutes=5)))
        decline_lapsed_requests(now=now)
        assert decline_lapsed_requests(now=now + timedelta(hours=1)) == []
        assert Notification.objects.filter(account=lapsed.volunteer_account, type="signup_declined").count() == 1

    def test_leaves_future_requests_and_decided_rows_alone(self, shelter):
        now = timezone.now()
        future = _signup(_shift(shelter, ends_at=now + timedelta(seconds=1)))
        past = _shift(shelter, ends_at=now - timedelta(hours=1))
        approved = _signup(past, status=SignupStatus.APPROVED)
        cancelled = _signup(past, status=SignupStatus.CANCELLED)

        assert decline_lapsed_requests(now=now) == []
        for su, status in ((future, "requested"), (approved, "approved"), (cancelled, "cancelled")):
            su.refresh_from_db()
            assert su.status == status
            assert su.end_reason == ""

    def test_a_shelter_decline_leaves_no_end_reason(self, client, shelter):
        su = _signup(_shift(shelter, ends_at=timezone.now() + timedelta(days=1)))
        r = client.post(f"/api/v1/shelter/signups/{su.pk}/decline", **_hdr(shelter))
        assert r.status_code == 200, r.content
        su.refresh_from_db()
        assert (su.status, su.end_reason) == ("declined", "")

    def test_after_the_sweep_home_and_inbox_both_say_zero(self, client, shelter):
        _signup(_shift(shelter, ends_at=timezone.now() - timedelta(hours=1)))
        decline_lapsed_requests()
        assert client.get(DASH, **_hdr(shelter)).json()["volunteer"]["pending_requests"] == 0
        assert client.get(INBOX, {"kind": "volunteer", "status": "open"}, **_hdr(shelter)).json()["results"] == []

    def test_runs_on_the_hourly_tick(self):
        assert ("requests_lapsed", decline_lapsed_requests) in SWEEPS
