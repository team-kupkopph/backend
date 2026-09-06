import pytest
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from listings.models import AdoptionInquiry, AdoptionListing, AdoptionStage, AdoptionStageKey, Pet, StageState


def _c(a):
    c = APIClient(); c.force_authenticate(user=a); return c


def _placement(recipient):
    poster = AccountFactory()
    listing = AdoptionListing.objects.create(posted_by=poster, species="dog", name="Rex",
        city="Manila", adoption_fee="0", status="pending")
    inq = AdoptionInquiry.objects.create(listing=listing, adopter_account=recipient, status="active")
    for key in AdoptionStageKey:
        AdoptionStage.objects.create(inquiry=inq, stage_key=key, state=StageState.SKIPPED)
    return listing, inq


@pytest.mark.django_db
def test_accept_creates_pet_and_marks_adopted():
    recipient = AccountFactory(); listing, inq = _placement(recipient)
    res = _c(recipient).post(f"/api/v1/inquiries/{inq.pk}/accept")
    assert res.status_code == 200 and "pet_id" in res.json()
    inq.refresh_from_db(); listing.refresh_from_db()
    assert inq.status == "adopted" and listing.status == "adopted"
    assert Pet.objects.filter(owner_account=recipient).count() == 1   # first Pet ever written


@pytest.mark.django_db
def test_decline_creates_no_pet_and_frees_listing():
    recipient = AccountFactory(); listing, inq = _placement(recipient)
    res = _c(recipient).post(f"/api/v1/inquiries/{inq.pk}/decline")
    assert res.status_code == 200
    inq.refresh_from_db(); listing.refresh_from_db()
    assert inq.status == "declined" and listing.status == "available"
    assert not Pet.objects.filter(owner_account=recipient).exists()


@pytest.mark.django_db
def test_accept_non_adopter_403():
    recipient = AccountFactory(); _, inq = _placement(recipient)
    assert _c(AccountFactory()).post(f"/api/v1/inquiries/{inq.pk}/accept").status_code == 403


@pytest.mark.django_db
def test_accept_on_normal_inquiry_409_not_a_placement():
    adopter = AccountFactory()
    listing = AdoptionListing.objects.create(posted_by=AccountFactory(), species="dog", name="Rex",
        city="Manila", adoption_fee="0", status="available")
    inq = AdoptionInquiry.objects.create(listing=listing, adopter_account=adopter, status="active")
    for key in AdoptionStageKey:   # a NORMAL inquiry: inquiry-stage done, rest not_started
        AdoptionStage.objects.create(inquiry=inq, stage_key=key,
            state=StageState.DONE if key == AdoptionStageKey.INQUIRY else StageState.NOT_STARTED)
    res = _c(adopter).post(f"/api/v1/inquiries/{inq.pk}/accept")
    assert res.status_code == 409 and res.json()["error"]["code"] == "not_a_placement"


@pytest.mark.django_db
def test_second_accept_409_already_decided_no_duplicate_pet():
    recipient = AccountFactory(); listing, inq = _placement(recipient)
    first = _c(recipient).post(f"/api/v1/inquiries/{inq.pk}/accept")
    assert first.status_code == 200
    second = _c(recipient).post(f"/api/v1/inquiries/{inq.pk}/accept")
    assert second.status_code == 409 and second.json()["error"]["code"] == "already_decided"
    assert Pet.objects.filter(owner_account=recipient).count() == 1


@pytest.mark.django_db
def test_decline_after_accept_409_already_decided():
    recipient = AccountFactory(); listing, inq = _placement(recipient)
    first = _c(recipient).post(f"/api/v1/inquiries/{inq.pk}/accept")
    assert first.status_code == 200
    second = _c(recipient).post(f"/api/v1/inquiries/{inq.pk}/decline")
    assert second.status_code == 409 and second.json()["error"]["code"] == "already_decided"
    inq.refresh_from_db(); listing.refresh_from_db()
    assert inq.status == "adopted" and listing.status == "adopted"
    assert Pet.objects.filter(owner_account=recipient).exists()


@pytest.mark.django_db
def test_accept_links_listing_to_pet_and_recipient():
    recipient = AccountFactory(); listing, inq = _placement(recipient)
    res = _c(recipient).post(f"/api/v1/inquiries/{inq.pk}/accept")
    assert res.status_code == 200
    pet_id = res.json()["pet_id"]
    listing.refresh_from_db()
    assert str(listing.adopted_pet_id) == pet_id
    assert listing.adopted_by_account_id == recipient.pk
