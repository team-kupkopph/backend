"""IB19 · a badge says when it was earned, not when we noticed.

`earned_at` was the award time, so a badge the sweep caught up on (a hook that missed, a backfill) read as earned today. It is the time of the event
that qualified the person now: the shift that was the Nth to finish, the rescue's resolution, the pledge's confirmation, the adoption. Owner decision
2026-11-06. A rehome has no `adopted_at`, so the listing's last update stands in for it (the best the data has).
"""
from datetime import timedelta

import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone

from accounts.factories import AccountFactory
from community.badges import award_badges_for
from community.models import AccountBadge, NeedPledge, PledgeStatus, ShelterNeed
from listings.models import AdoptionListing, ListingStatus
from sagip.models import RescueCase, StrayReport, StrayStatus
from volunteer.models import SignupStatus, VolunteerShift, VolunteerSignup


def _ago(days):
    return timezone.now() - timedelta(days=days)


def _earned(account, code):
    return AccountBadge.objects.get(account=account, badge_id=code).earned_at


def _shift(volunteer, ends_at):
    shelter = AccountFactory(account_type="shelter")
    shift = VolunteerShift.objects.create(shelter_account=shelter, starts_at=ends_at - timedelta(hours=2), ends_at=ends_at)
    return VolunteerSignup.objects.create(shift=shift, volunteer_account=volunteer, status=SignupStatus.COMPLETED, waiver_accepted=True)


def _rescue(claimer, resolved_at):
    report = StrayReport.objects.create(reporter_account=AccountFactory(), species="dog", condition="injured",
                                        status=StrayStatus.RESOLVED, geom=Point(121.05, 14.63, srid=4326))
    return RescueCase.objects.create(report=report, claimed_by_account=claimer, resolved_at=resolved_at, outcome_notes="Reunited with the owner.")


def _rehome(poster, when):
    listing = AdoptionListing.objects.create(posted_by=poster, name="Rex", species="dog", status=ListingStatus.ADOPTED,
                                             adopted_by_account=AccountFactory())
    AdoptionListing.objects.filter(pk=listing.pk).update(updated_at=when)       # `updated_at` is auto_now: only an update can set it
    return listing


def _pledge(giver, decided_at):
    need = ShelterNeed.objects.create(shelter_account=AccountFactory(account_type="shelter"), title="Kibble", category="food",
                                      quantity_needed=5)
    return NeedPledge.objects.create(need=need, pledger_account=giver, quantity=1, status=PledgeStatus.DELIVERED, decided_at=decided_at)


@pytest.mark.django_db
def test_a_late_shift_badge_is_dated_by_the_shift_that_earned_it():
    volunteer = AccountFactory()
    ends = [_ago(30 - i) for i in range(10)]                    # ten shifts, a day apart, the last one 21 days ago
    for when in ends:
        _shift(volunteer, when)
    assert sorted(award_badges_for(volunteer)) == ["first_shift", "shifts_10"]
    assert _earned(volunteer, "first_shift") == ends[0]
    assert _earned(volunteer, "shifts_10") == ends[9]


@pytest.mark.django_db
def test_a_rescue_badge_is_dated_by_the_resolution():
    claimer = AccountFactory()
    when = _ago(5)
    _rescue(claimer, when)
    award_badges_for(claimer)
    assert _earned(claimer, "first_rescue") == when


@pytest.mark.django_db
def test_a_pledge_badge_is_dated_by_the_shelters_confirmation():
    giver = AccountFactory()
    when = _ago(3)
    _pledge(giver, when)
    award_badges_for(giver)
    assert _earned(giver, "first_pledge") == when


@pytest.mark.django_db
def test_a_rehome_badge_is_dated_by_the_adoption():
    poster = AccountFactory()
    when = _ago(4)
    _rehome(poster, when)
    award_badges_for(poster)
    assert _earned(poster, "rehomed_1") == AdoptionListing.objects.get(posted_by=poster).updated_at


@pytest.mark.django_db
def test_bayani_is_dated_by_the_last_of_its_three_firsts():
    person = AccountFactory()
    _shift(person, _ago(20))
    _rescue(person, _ago(9))
    _rehome(person, _ago(12))
    award_badges_for(person)
    assert abs(_earned(person, "bayani") - _ago(9)) < timedelta(seconds=5)       # the rescue came last


@pytest.mark.django_db
def test_a_badge_is_never_dated_in_the_future():
    volunteer = AccountFactory()
    _shift(volunteer, timezone.now() + timedelta(days=2))        # a completed shift whose end is still ahead (a clock or data fault)
    award_badges_for(volunteer)
    assert _earned(volunteer, "first_shift") <= timezone.now()


@pytest.mark.django_db
def test_a_badge_earned_just_now_is_dated_just_now():
    volunteer = AccountFactory()
    _shift(volunteer, timezone.now() - timedelta(minutes=1))
    award_badges_for(volunteer)
    assert timezone.now() - _earned(volunteer, "first_shift") < timedelta(minutes=2)
