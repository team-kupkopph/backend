"""US-B1 · badge awarding + impact.

D-S6-2 shift-agnostic codes; D-S6-3 idempotent event+sweep awarding; D-S6-5 badge_earned is
in-app only (push:false).
"""
import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from community.badges import award_badges_for
from community.models import AccountBadge
from listings.models import AdoptionListing, ListingStatus
from notifications.models import Notification
from notifications.types import REGISTRY
from sagip.models import RescueCase, StrayReport
from volunteer.models import SignupStatus, VolunteerShift, VolunteerSignup


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _complete_shifts(account, n):
    shelter = AccountFactory(account_type="shelter")
    now = timezone.now()
    for i in range(n):
        shift = VolunteerShift.objects.create(
            shelter_account=shelter, starts_at=now - timezone.timedelta(days=i + 1),
            ends_at=now - timezone.timedelta(days=i + 1) + timezone.timedelta(hours=2))
        VolunteerSignup.objects.create(shift=shift, volunteer_account=account,
                                       status=SignupStatus.COMPLETED, waiver_accepted=True)


def _resolve_case(account):
    report = StrayReport.objects.create(species="dog", condition="injured", status="resolved",
                                        geom=Point(121.05, 14.63, srid=4326))
    RescueCase.objects.create(report=report, claimed_by_account=account,
                              resolved_at=timezone.now())


def _rehome(account, n):
    for _ in range(n):
        AdoptionListing.objects.create(posted_by=account, name="Rex", species="dog",
                                       status=ListingStatus.ADOPTED)


@pytest.mark.django_db
def test_one_completed_shift_earns_first_shift(client):
    acc = AccountFactory()
    _complete_shifts(acc, 1)
    awarded = award_badges_for(acc)
    assert "first_shift" in awarded
    assert AccountBadge.objects.filter(account=acc, badge_id="first_shift").exists()
    assert Notification.objects.filter(account=acc, type="badge_earned").count() == 1


@pytest.mark.django_db
def test_ten_shifts_earn_the_steady_badge(client):
    acc = AccountFactory()
    _complete_shifts(acc, 10)
    awarded = award_badges_for(acc)
    assert {"first_shift", "shifts_10"} <= set(awarded)


@pytest.mark.django_db
def test_a_resolved_rescue_earns_first_rescue(client):
    acc = AccountFactory()
    _resolve_case(acc)
    assert "first_rescue" in award_badges_for(acc)


@pytest.mark.django_db
def test_rehoming_five_earns_both_rehome_badges(client):
    acc = AccountFactory()
    _rehome(acc, 5)
    assert {"rehomed_1", "rehomed_5"} <= set(award_badges_for(acc))


@pytest.mark.django_db
def test_bayani_needs_a_shift_a_rescue_and_a_rehome(client):
    acc = AccountFactory()
    _complete_shifts(acc, 1)
    _resolve_case(acc)
    assert "bayani" not in award_badges_for(acc)   # no rehome yet
    _rehome(acc, 1)
    assert "bayani" in award_badges_for(acc)


@pytest.mark.django_db
def test_awarding_is_idempotent(client):
    acc = AccountFactory()
    _complete_shifts(acc, 1)
    first = award_badges_for(acc)
    second = award_badges_for(acc)
    assert "first_shift" in first and second == []          # nothing new the second time
    assert AccountBadge.objects.filter(account=acc, badge_id="first_shift").count() == 1
    assert Notification.objects.filter(account=acc, type="badge_earned").count() == 1


def test_badge_earned_is_in_app_only():
    # D-S6-5: no push for a badge.
    assert REGISTRY["badge_earned"].push is False


@pytest.mark.django_db
def test_me_impact_returns_badges_and_aggregates(client):
    acc = AccountFactory()
    _complete_shifts(acc, 2)
    _rehome(acc, 1)
    award_badges_for(acc)
    res = client.get("/api/v1/me/impact", **_hdr(acc))
    assert res.status_code == 200
    body = res.json()
    assert body["impact"]["shifts_completed"] == 2
    assert body["impact"]["pets_rehomed"] == 1
    # the full catalog is returned with earned flags (US-B2 grid: earned bright, rest dimmed)
    by_code = {b["badge_code"]: b for b in body["badges"]}
    assert by_code["first_shift"]["earned"] is True
    assert by_code["first_shift"]["earned_at"] is not None
    assert by_code["shifts_50"]["earned"] is False          # unearned, but present with criteria
    assert by_code["shifts_50"]["criteria"]
    assert body["badges"][0]["earned"] is True              # earned sort first


@pytest.mark.django_db
def test_attendance_completed_awards_a_badge(client):
    """The event hook: marking a shift Attended earns first_shift without any sweep."""
    volunteer = AccountFactory()
    shelter = AccountFactory(account_type="shelter")
    now = timezone.now()
    shift = VolunteerShift.objects.create(
        shelter_account=shelter, starts_at=now - timezone.timedelta(hours=3),
        ends_at=now - timezone.timedelta(hours=1))
    su = VolunteerSignup.objects.create(shift=shift, volunteer_account=volunteer,
                                        status=SignupStatus.APPROVED, waiver_accepted=True)
    res = client.post(f"/api/v1/shelter/signups/{su.pk}/attendance", {"outcome": "completed"},
                      content_type="application/json", **_hdr(shelter))
    assert res.status_code == 200
    assert AccountBadge.objects.filter(account=volunteer, badge_id="first_shift").exists()


@pytest.mark.django_db
def test_the_sweep_reconciles_missed_badges(client):
    """A completed shift written directly (no event hook) is still badged by the nightly sweep."""
    from community.sweeps import award_badges
    acc = AccountFactory()
    _complete_shifts(acc, 1)
    assert not AccountBadge.objects.filter(account=acc).exists()
    touched = award_badges()
    assert any(aid == acc.pk for aid, _ in touched)
    assert AccountBadge.objects.filter(account=acc, badge_id="first_shift").exists()
