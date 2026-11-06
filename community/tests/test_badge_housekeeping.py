"""Impact & badges step 20 (IB22, IB27): retiring a badge, and one notice for a catalog rollout.

IB19 (the event's own time on `earned_at`) and the composite primary key (IB21) are decisions, not built: see the model.
"""
import pytest
from django.test import Client
from django.utils import timezone

from accounts.factories import AccountFactory
from community.badges import award_badges_for, reconcile_shift_badges
from community.models import Badge
from community.tests.test_badges_safe_award import _completed_shift
from community.tests.test_badges_undo import _held, _notices
from community.tests.test_needs import _hdr
from volunteer.models import VolunteerSignup


def _volunteer_with_shifts(n):
    volunteer = AccountFactory()
    for _ in range(n):
        _completed_shift(volunteer)
    return volunteer


# ── IB27 · a rollout is one notice, not one per badge ───────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_several_badges_at_once_are_one_notice():
    volunteer = _volunteer_with_shifts(10)
    assert sorted(award_badges_for(volunteer)) == ["first_shift", "shifts_10"]
    notices = _notices(volunteer)
    assert notices.count() == 1
    note = notices.get()
    assert "First Kawang-Gawa" in note.body and "Steady Volunteer" in note.body
    assert sorted(note.data["badge_codes"]) == ["first_shift", "shifts_10"]


@pytest.mark.django_db
def test_one_badge_is_still_the_one_badge_notice():
    volunteer = _volunteer_with_shifts(1)
    assert award_badges_for(volunteer) == ["first_shift"]
    assert _notices(volunteer).get().data == {"badge_code": "first_shift"}


# ── IB22 · a retired badge is not awarded, and a holder keeps it ────────────────────────────────────────────────
@pytest.mark.django_db
def test_a_retired_badge_is_not_awarded():
    Badge.objects.filter(pk="first_shift").update(retired_at=timezone.now())
    volunteer = _volunteer_with_shifts(1)
    assert award_badges_for(volunteer) == []
    assert _held(volunteer) == set() and _notices(volunteer).count() == 0


@pytest.mark.django_db
def test_a_holder_keeps_a_retired_badge_and_still_sees_it():
    volunteer = _volunteer_with_shifts(1)
    award_badges_for(volunteer)
    Badge.objects.filter(pk="first_shift").update(retired_at=timezone.now())
    codes = {b["badge_code"]: b for b in Client().get("/api/v1/me/impact", **_hdr(volunteer)).json()["badges"]}
    assert "first_shift" in codes and codes["first_shift"]["earned"] is True
    assert _held(volunteer) == {"first_shift"}


@pytest.mark.django_db
def test_a_retired_badge_nobody_holds_is_not_in_the_catalog_the_app_shows():
    Badge.objects.filter(pk="shifts_50").update(retired_at=timezone.now())
    stranger = AccountFactory()
    codes = [b["badge_code"] for b in Client().get("/api/v1/me/impact", **_hdr(stranger)).json()["badges"]]
    assert "shifts_50" not in codes and "first_shift" in codes


# ── IB27 × IB1 · an undo takes a revoked badge out of a shared notice ───────────────────────────────────────────
@pytest.mark.django_db
def test_an_undo_that_revokes_one_badge_leaves_the_notice_for_the_other():
    volunteer = _volunteer_with_shifts(10)
    award_badges_for(volunteer)
    VolunteerSignup.objects.filter(volunteer_account=volunteer).order_by("-created_at").first().delete()
    assert reconcile_shift_badges(volunteer) == ["shifts_10"]
    note = _notices(volunteer).get()
    assert note.data == {"badge_code": "first_shift"} and "Steady Volunteer" not in note.body


@pytest.mark.django_db
def test_an_undo_that_revokes_every_badge_in_a_shared_notice_removes_it():
    volunteer = _volunteer_with_shifts(10)
    award_badges_for(volunteer)
    VolunteerSignup.objects.filter(volunteer_account=volunteer).delete()
    assert sorted(reconcile_shift_badges(volunteer)) == ["first_shift", "shifts_10"]
    assert _notices(volunteer).count() == 0
