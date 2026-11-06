"""NT-9 / F13 · staff take a badge back for confirmed fraud, and it stays taken.

Owner decisions 2026-11-06: the holder is not told (silent, as an attendance undo is), and the sweep must not award it again while the person still meets the
rule, so a revocation is a row that outlives the evidence.
"""
from unittest import mock

import pytest
from django.test import Client

from accounts.factories import AccountFactory
from community.badges import award_badges_for, revoke_badge
from community.models import BadgeRevocation
from community.sweeps import award_badges
from community.tests.test_badges_safe_award import _completed_shift
from community.tests.test_badges_undo import _held, _notices
from community.tests.test_needs import _hdr


def _holder(shifts=1):
    account = AccountFactory()
    for _ in range(shifts):
        _completed_shift(account)
    award_badges_for(account)
    return account


@pytest.mark.django_db
def test_revoking_removes_the_badge_and_its_notice_silently():
    holder = _holder()
    assert _held(holder) == {"first_shift"} and _notices(holder).count() == 1
    staff = AccountFactory(account_type="admin")
    revoke_badge(holder, "first_shift", reason="Shifts were fabricated.", staff=staff)
    assert _held(holder) == set()
    assert _notices(holder).count() == 0                                   # nothing is left saying they earned it
    row = BadgeRevocation.objects.get(account=holder, badge_id="first_shift")
    assert row.reason == "Shifts were fabricated." and row.revoked_by == staff


@pytest.mark.django_db
def test_the_holder_is_sent_nothing():
    holder = _holder()
    from notifications.models import Notification
    before = Notification.objects.filter(account=holder).count()
    revoke_badge(holder, "first_shift", reason="x", staff=None)
    assert Notification.objects.filter(account=holder).count() == before - 1        # only the old earned notice went; no new one


@pytest.mark.django_db
def test_a_revoked_badge_is_not_awarded_again_by_the_next_award_or_the_sweep():
    holder = _holder()
    revoke_badge(holder, "first_shift", reason="x", staff=None)
    assert award_badges_for(holder) == []
    touched, failed = award_badges()
    assert touched == [] and failed == []
    assert _held(holder) == set() and _notices(holder).count() == 0


@pytest.mark.django_db
def test_other_badges_are_still_awarded():
    holder = _holder(shifts=10)                                             # first_shift and shifts_10
    revoke_badge(holder, "shifts_10", reason="x", staff=None)
    assert _held(holder) == {"first_shift"}
    _completed_shift(holder)
    assert award_badges_for(holder) == []                                   # shifts_10 stays revoked; nothing new to earn
    assert _held(holder) == {"first_shift"}


@pytest.mark.django_db
def test_revoking_one_badge_of_a_shared_notice_leaves_the_notice_for_the_other():
    account = AccountFactory()
    for _ in range(10):
        _completed_shift(account)
    award_badges_for(account)                                               # one shared notice for both
    revoke_badge(account, "shifts_10", reason="x", staff=None)
    note = _notices(account).get()
    assert note.data == {"badge_code": "first_shift"}


@pytest.mark.django_db
def test_a_badge_not_held_cannot_be_revoked():
    stranger = AccountFactory()
    with pytest.raises(LookupError):
        revoke_badge(stranger, "first_shift", reason="x", staff=None)
    assert not BadgeRevocation.objects.exists()


@pytest.mark.django_db
def test_an_unknown_badge_cannot_be_revoked():
    with pytest.raises(LookupError):
        revoke_badge(AccountFactory(), "no_such_badge", reason="x", staff=None)


@pytest.mark.django_db
def test_a_revoked_badge_is_gone_from_that_persons_impact_screen_and_only_theirs():
    holder, other = _holder(), _holder()
    revoke_badge(holder, "first_shift", reason="x", staff=None)
    mine = [b["badge_code"] for b in Client().get("/api/v1/me/impact", **_hdr(holder)).json()["badges"]]
    theirs = [b["badge_code"] for b in Client().get("/api/v1/me/impact", **_hdr(other)).json()["badges"]]
    assert "first_shift" not in mine and "first_shift" in theirs


@pytest.mark.django_db
def test_the_revocation_is_an_event():
    holder = _holder()
    with mock.patch("community.badges.emit") as emit:
        revoke_badge(holder, "first_shift", reason="x", staff=None)
    emit.assert_any_call("badge_revoked", badge_code="first_shift", reason="staff")
