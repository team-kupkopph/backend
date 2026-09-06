"""US-A1 — Pet + the full adoption module: models, migration, and the
set_stage_state single-writer (mirrors sagip's set_report_status pattern)."""
import pytest
from django.db import IntegrityError, transaction

from accounts.factories import AccountFactory
from listings.models import (
    AdoptionInquiry,
    AdoptionListing,
    AdoptionStage,
    AdoptionStageHistory,
    AdoptionStageKey,
    Pet,
    PetPhoto,
    StageState,
)
from listings.stages import StageError, set_stage_state


@pytest.mark.django_db
def test_pet_can_be_created_with_only_the_required_fields():
    owner = AccountFactory()
    pet = Pet.objects.create(owner_account=owner, name="Bantay", species="dog")
    assert pet.sex == "unknown"          # DDL default
    assert pet.dob_approximate is False  # DDL default


@pytest.mark.django_db
def test_pet_photos_cascade_from_the_pet():
    pet = Pet.objects.create(owner_account=AccountFactory(), name="Muning", species="cat")
    PetPhoto.objects.create(pet=pet, url="https://example.invalid/p1")
    assert pet.photos.count() == 1


@pytest.mark.django_db
def test_adoption_listing_carries_the_full_ddl_column_set():
    poster = AccountFactory()
    listing = AdoptionListing.objects.create(
        posted_by=poster, name="Ana", species="dog", city="Marikina",
        sex="female", dob_approximate=True, size_category="medium",
        adoption_fee="500.00", walkable=True)
    listing.refresh_from_db()
    assert listing.status == "available"          # renamed from listing_status, default intact
    assert str(listing.adoption_fee) == "500.00"
    assert listing.walkable is True
    assert listing.source_report is None and listing.adopted_pet is None  # nullable, unused this sprint


@pytest.mark.django_db
def test_a_second_inquiry_from_the_same_adopter_on_the_same_listing_is_rejected():
    listing = AdoptionListing.objects.create(posted_by=AccountFactory(), name="Bantay",
                                             species="dog", city="Marikina")
    adopter = AccountFactory()
    AdoptionInquiry.objects.create(listing=listing, adopter_account=adopter)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            AdoptionInquiry.objects.create(listing=listing, adopter_account=adopter)


@pytest.mark.django_db
def test_a_stage_key_can_only_appear_once_per_inquiry():
    listing = AdoptionListing.objects.create(posted_by=AccountFactory(), name="Bantay",
                                             species="dog", city="Marikina")
    inquiry = AdoptionInquiry.objects.create(listing=listing, adopter_account=AccountFactory())
    AdoptionStage.objects.create(inquiry=inquiry, stage_key=AdoptionStageKey.APPLICATION)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            AdoptionStage.objects.create(inquiry=inquiry, stage_key=AdoptionStageKey.APPLICATION)


@pytest.mark.django_db
def test_set_stage_state_moves_the_stage_and_logs_it():
    listing = AdoptionListing.objects.create(posted_by=AccountFactory(), name="Bantay",
                                             species="dog", city="Marikina")
    inquiry = AdoptionInquiry.objects.create(listing=listing, adopter_account=AccountFactory())
    stage = AdoptionStage.objects.create(inquiry=inquiry, stage_key=AdoptionStageKey.APPLICATION)
    poster = listing.posted_by

    row = set_stage_state(stage, StageState.IN_PROGRESS, poster, note="Reviewing the form")
    stage.refresh_from_db()

    assert stage.state == StageState.IN_PROGRESS and stage.note == "Reviewing the form"
    assert row.state == StageState.IN_PROGRESS and row.changed_by_account_id == poster.pk
    assert AdoptionStageHistory.objects.filter(inquiry=inquiry).count() == 1


@pytest.mark.django_db
def test_set_stage_state_rejects_an_unknown_state():
    listing = AdoptionListing.objects.create(posted_by=AccountFactory(), name="Bantay",
                                             species="dog", city="Marikina")
    inquiry = AdoptionInquiry.objects.create(listing=listing, adopter_account=AccountFactory())
    stage = AdoptionStage.objects.create(inquiry=inquiry, stage_key=AdoptionStageKey.APPLICATION)
    with pytest.raises(StageError):
        set_stage_state(stage, "teleported", AccountFactory())
    stage.refresh_from_db()
    assert stage.state == StageState.NOT_STARTED
    assert AdoptionStageHistory.objects.filter(inquiry=inquiry).count() == 0


@pytest.mark.django_db
def test_stage_history_survives_the_actor_being_deleted():
    listing = AdoptionListing.objects.create(posted_by=AccountFactory(), name="Bantay",
                                             species="dog", city="Marikina")
    inquiry = AdoptionInquiry.objects.create(listing=listing, adopter_account=AccountFactory())
    stage = AdoptionStage.objects.create(inquiry=inquiry, stage_key=AdoptionStageKey.APPLICATION)
    actor = AccountFactory()
    set_stage_state(stage, StageState.DONE, actor)
    actor.delete()
    row = AdoptionStageHistory.objects.get(inquiry=inquiry)
    assert row.changed_by_account_id is None  # SET_NULL — the audit row is kept
