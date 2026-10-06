"""Flow 25 (the poster screens an applicant) needs a shelter listing with one fresh inquiry from
the e2e owner, reset every run because the flow completes the adoption."""
import e2e_fixtures
import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from listings.models import AdoptionInquiry, AdoptionStage, ListingStatus
from shelter.models import ShelterProfile
from verifications.models import AccountCapability

pytestmark = pytest.mark.django_db


def _people():
    owner = AccountFactory(phone="+639170000123", phone_verified_at=timezone.now())
    AccountCapability.objects.create(account=owner, capability="rescuer", status="approved")
    shelter = AccountFactory(account_type="shelter")
    ShelterProfile.objects.create(account=shelter, org_name="E2E Test Shelter", org_type="shelter",
                                  tier="community_rescue")
    return owner, shelter


def test_seeds_one_fresh_inquiry_and_resets_on_rerun():
    owner, shelter = _people()
    first = e2e_fixtures.seed_adoption_applicant(owner, shelter)
    assert first.listing.name == "E2E Milo" and first.listing.status == ListingStatus.AVAILABLE
    assert first.status == "active" and first.accepted_at is None
    assert AdoptionStage.objects.filter(inquiry=first).count() == 6
    assert AdoptionStage.objects.get(inquiry=first, stage_key="inquiry").state == "done"
    # the flow completes it; a re-run must give a clean slate on the same listing
    AdoptionInquiry.objects.filter(pk=first.pk).update(status="adopted")
    type(first.listing).objects.filter(pk=first.listing.pk).update(status=ListingStatus.ADOPTED)
    second = e2e_fixtures.seed_adoption_applicant(owner, shelter)
    assert second.listing.pk == first.listing.pk and second.pk != first.pk
    assert second.listing.status == ListingStatus.AVAILABLE
    assert AdoptionInquiry.objects.filter(listing=second.listing).count() == 1
