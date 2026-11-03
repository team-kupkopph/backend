"""My Pets test plan U11 / NT-2 (dev/test-plan-my-pets.md), decided 2026-11-03: Complete on a rehoming
listing whose pet has been deleted is UNREACHABLE, so there is no refusal to build. These tests pin why.

Only the account purge deletes a pet (an owner's remove archives it, and is refused while it is listed).
The purge runs only on an account deleted 30 days before, whose poster can no longer sign in, and only
the poster can Complete a public inquiry. An active inquiry blocks the deletion in the first place
(F3, accounts/tests/test_account_deletion.py), and nobody can inquire on a deleted poster's listing.
Placements are never rehoming listings.

The one loose end, tidied: the purge withdraws the open rehoming listing whose pet it deletes, so no
open listing is left describing a pet that no longer exists (`rehome_pet` is SET_NULL)."""
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.lifecycle import soft_delete_account
from accounts.purge import anonymize_expired_accounts
from listings.models import AdoptionListing, ListingStatus
from listings.tests.adoption_helpers import c, person
from pets.models import Pet


def _listed_pet(owner, publish=True):
    pid = c(owner).post("/api/v1/me/pets", {"name": "Mochi", "species": "cat"}, format="json").json()["pet_id"]
    res = c(owner).post("/api/v1/listings", {"pet": {"name": "Mochi", "species": "cat"}, "adoption_fee": "0",
                                             "city": "Marikina", "pet_id": pid}, format="json")
    assert res.status_code == 201, res.content
    lid = res.json()["listing_id"]
    if publish:
        assert c(owner).post(f"/api/v1/listings/{lid}/publish").status_code == 200
    return pid, lid


def _deleted_long_ago(account):
    soft_delete_account(account)
    account.deleted_at = timezone.now() - timedelta(days=31)
    account.save(update_fields=["deleted_at"])


@pytest.mark.django_db
def test_nobody_can_inquire_on_a_deleted_posters_rehoming_listing():
    owner, adopter = person(member=True), person(member=True)
    _, lid = _listed_pet(owner)
    soft_delete_account(owner)
    res = c(adopter).post(f"/api/v1/listings/{lid}/inquiries", {"message": "Hi"}, format="json")
    assert res.status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize("publish", [True, False], ids=["available", "draft"])
def test_the_purge_withdraws_the_listing_of_the_pet_it_deletes(publish):
    owner = person(member=True)
    pid, lid = _listed_pet(owner, publish=publish)
    _deleted_long_ago(owner)

    anonymize_expired_accounts()

    assert not Pet.objects.filter(pk=pid).exists()
    listing = AdoptionListing.objects.get(pk=lid)
    assert listing.status == ListingStatus.WITHDRAWN
    assert listing.rehome_pet_id is None


@pytest.mark.django_db
def test_the_purge_leaves_an_adopted_listing_and_other_posters_listings_alone():
    owner, other = person(member=True), person(member=True)
    _, adopted = _listed_pet(owner)
    AdoptionListing.objects.filter(pk=adopted).update(status=ListingStatus.ADOPTED)
    _, kept = _listed_pet(owner)
    AdoptionListing.objects.filter(pk=kept).update(rehome_pet=None)      # not a rehoming listing any more
    _, theirs = _listed_pet(other)
    _deleted_long_ago(owner)

    anonymize_expired_accounts()

    assert AdoptionListing.objects.get(pk=adopted).status == ListingStatus.ADOPTED
    assert AdoptionListing.objects.get(pk=kept).status == ListingStatus.AVAILABLE
    assert AdoptionListing.objects.get(pk=theirs).status == ListingStatus.AVAILABLE
