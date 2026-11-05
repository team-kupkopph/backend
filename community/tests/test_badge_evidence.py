"""IB9 / IB10 · what counts as a rehome and as a rescue (owner decisions IQ2 and IQ3, 2026-11-05).

IQ2: a rehome has a second party. `mark-adopted` (the animal "found a home outside Kupkop")
has no adopter, so a verified poster could mark five listings adopted and hold Rehome Hero.
IQ3: a rescue leaves evidence: an outcome note or photo, or an adoption through Kupkop. And a
report moderators took down no longer counts.

Badges already held are kept (D13's rule, the owner's "keep existing badges").
"""
import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone

from accounts.factories import AccountFactory
from community.badges import award_badges_for, impact_counts
from community.models import AccountBadge, Badge
from community.sweeps import award_badges
from listings.models import AdoptionListing, ListingStatus
from listings.tests.adoption_helpers import c, listing, person
from sagip.models import RescueCase, StrayReport, StrayStatus


def _held(account):
    return set(AccountBadge.objects.filter(account=account).values_list("badge_id", flat=True))


def _case(claimer, *, notes="", photo="", reporter="other", hidden=False):
    report = StrayReport.objects.create(
        reporter_account=AccountFactory() if reporter == "other" else reporter,
        species="dog", condition="injured", status=StrayStatus.RESOLVED,
        geom=Point(121.05, 14.63, srid=4326), hidden_at=timezone.now() if hidden else None)
    case = RescueCase.objects.create(report=report, claimed_by_account=claimer,
                                     resolved_at=timezone.now(), outcome_notes=notes,
                                     outcome_photo_url=photo)
    return report, case


# ── IQ2 · rehomes ───────────────────────────────────────────────
@pytest.mark.django_db
def test_an_adoption_with_no_adopter_is_not_a_rehome():
    poster = AccountFactory()
    AdoptionListing.objects.create(posted_by=poster, name="Rex", species="dog",
                                   status=ListingStatus.ADOPTED)
    assert impact_counts(poster)["pets_rehomed"] == 0


@pytest.mark.django_db
def test_an_adoption_by_someone_else_is_a_rehome():
    poster = AccountFactory()
    AdoptionListing.objects.create(posted_by=poster, name="Rex", species="dog",
                                   status=ListingStatus.ADOPTED, adopted_by_account=AccountFactory())
    assert impact_counts(poster)["pets_rehomed"] == 1


@pytest.mark.django_db
def test_adopting_your_own_listing_is_not_a_rehome():
    poster = AccountFactory()
    AdoptionListing.objects.create(posted_by=poster, name="Rex", species="dog",
                                   status=ListingStatus.ADOPTED, adopted_by_account=poster)
    assert impact_counts(poster)["pets_rehomed"] == 0


@pytest.mark.django_db
def test_a_listing_that_is_not_adopted_is_not_a_rehome():
    poster = AccountFactory()
    AdoptionListing.objects.create(posted_by=poster, name="Rex", species="dog",
                                   status=ListingStatus.AVAILABLE, adopted_by_account=AccountFactory())
    assert impact_counts(poster)["pets_rehomed"] == 0


@pytest.mark.django_db
def test_marking_five_listings_adopted_earns_no_rehome_badge(django_capture_on_commit_callbacks):
    """The probe: create, publish, mark adopted, five times, and hold Rehome Hero."""
    poster = person(member=True)
    for _ in range(5):
        the_listing = listing(poster, status=ListingStatus.AVAILABLE)
        with django_capture_on_commit_callbacks(execute=True):
            assert c(poster).post(f"/api/v1/listings/{the_listing.pk}/mark-adopted").status_code == 200
    assert _held(poster) == set()
    assert c(poster).get("/api/v1/me/impact").json()["impact"]["pets_rehomed"] == 0


@pytest.mark.django_db
def test_a_rehome_badge_already_held_is_kept():
    poster = AccountFactory()
    AccountBadge.objects.create(account=poster, badge=Badge.objects.get(pk="rehomed_1"))
    AdoptionListing.objects.create(posted_by=poster, name="Rex", species="dog",
                                   status=ListingStatus.ADOPTED)
    award_badges_for(poster)
    award_badges()
    assert _held(poster) == {"rehomed_1"}


# ── IQ3 · rescues ───────────────────────────────────────────────
@pytest.mark.django_db
def test_a_rescue_with_no_note_or_photo_does_not_count():
    claimer = AccountFactory()
    _case(claimer)
    assert impact_counts(claimer)["rescues_helped"] == 0


@pytest.mark.django_db
def test_a_rescue_with_an_outcome_note_counts():
    claimer = AccountFactory()
    _case(claimer, notes="Safe with a neighbour")
    assert impact_counts(claimer)["rescues_helped"] == 1


@pytest.mark.django_db
def test_a_rescue_with_only_an_outcome_photo_counts():
    claimer = AccountFactory()
    _case(claimer, photo="https://example.invalid/outcome.jpg")
    assert impact_counts(claimer)["rescues_helped"] == 1


@pytest.mark.django_db
def test_a_rescue_whose_report_was_taken_down_does_not_count():
    claimer = AccountFactory()
    _case(claimer, notes="Safe", hidden=True)
    assert impact_counts(claimer)["rescues_helped"] == 0


@pytest.mark.django_db
def test_a_report_with_no_reporter_still_counts():
    """A deleted reporter is NULL, and NULL must not read as "your own report"."""
    claimer = AccountFactory()
    _case(claimer, notes="Safe", reporter=None)
    assert impact_counts(claimer)["rescues_helped"] == 1


@pytest.mark.django_db
def test_a_rescue_that_ended_in_an_adoption_through_kupkop_counts_without_a_note():
    """A rescuer who finds the animal a home resolves nothing by hand: the adoption resolves the
    report. Their evidence is the adoption, and asking for a note too would punish the best outcome."""
    claimer = AccountFactory()
    report, _ = _case(claimer)
    AdoptionListing.objects.create(posted_by=claimer, source_report=report, name="Bruno",
                                   species="dog", status=ListingStatus.ADOPTED,
                                   adopted_by_account=AccountFactory())
    assert impact_counts(claimer)["rescues_helped"] == 1


@pytest.mark.django_db
def test_an_adoption_with_no_adopter_is_not_evidence_of_a_rescue():
    claimer = AccountFactory()
    report, _ = _case(claimer)
    AdoptionListing.objects.create(posted_by=claimer, source_report=report, name="Bruno",
                                   species="dog", status=ListingStatus.ADOPTED)
    assert impact_counts(claimer)["rescues_helped"] == 0


@pytest.mark.django_db
def test_adopting_the_animal_you_rescued_is_not_evidence():
    claimer = AccountFactory()
    report, _ = _case(claimer)
    AdoptionListing.objects.create(posted_by=claimer, source_report=report, name="Bruno",
                                   species="dog", status=ListingStatus.ADOPTED,
                                   adopted_by_account=claimer)
    assert impact_counts(claimer)["rescues_helped"] == 0


@pytest.mark.django_db
def test_two_adoption_listings_do_not_count_one_rescue_twice():
    claimer = AccountFactory()
    report, _ = _case(claimer, notes="Safe")
    for _ in range(2):
        AdoptionListing.objects.create(posted_by=claimer, source_report=report, name="Bruno",
                                       species="dog", status=ListingStatus.ADOPTED,
                                       adopted_by_account=AccountFactory())
    assert impact_counts(claimer)["rescues_helped"] == 1


@pytest.mark.django_db
def test_resolving_with_no_outcome_earns_no_rescue_badge(django_capture_on_commit_callbacks):
    """The probe: one call from claimed to resolved, and a badge."""
    claimer = AccountFactory()
    report = StrayReport.objects.create(reporter_account=AccountFactory(), species="dog",
                                        condition="injured", status=StrayStatus.CLAIMED,
                                        geom=Point(121.05, 14.63, srid=4326))
    case = RescueCase.objects.create(report=report, claimed_by_account=claimer)
    with django_capture_on_commit_callbacks(execute=True):
        res = c(claimer).post(f"/api/v1/cases/{case.pk}/status", {"status": "resolved"},
                              format="json")
    assert res.status_code == 200 and _held(claimer) == set()


@pytest.mark.django_db
def test_a_rescue_badge_already_held_survives_a_takedown():
    claimer = AccountFactory()
    report, _ = _case(claimer, notes="Safe")
    award_badges_for(claimer)
    assert _held(claimer) == {"first_rescue"}
    StrayReport.objects.filter(pk=report.pk).update(hidden_at=timezone.now())
    award_badges_for(claimer)
    award_badges()
    assert _held(claimer) == {"first_rescue"}
