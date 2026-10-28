"""AQ8 / AD24 · owners can add a pet, list one of their own for adoption, and on Complete the Pet
row changes hands (history kept) instead of a fresh one being made."""
import pytest

from listings.models import AdoptionListing, ListingStatus, Pet, PetOwnershipHistory
from listings.tests.adoption_helpers import c, inquire, listing, person, post

BODY = {"name": "Mochi", "species": "cat", "breed": "Puspin", "sex": "female",
        "birthdate": "2022-03-01", "size_category": "small", "spayed_neutered": True,
        "temperament": "Lap cat", "medical_conditions": "Mild asthma", "vet_contact": "Dr Reyes 0917"}


def own_photo(owner, name):
    """PP5 · a pet photo must be the owner's own `listing_photo` upload (common/uploads.py)."""
    return f"https://bkt.example/listing_photo/{owner.pk}/{name}"


def _add(owner, body=None):
    if body is None:
        body = {**BODY, "photos": [{"file_url": own_photo(owner, "m1")},
                                   {"file_url": own_photo(owner, "m2"), "is_primary": True}]}
    return c(owner).post("/api/v1/me/pets", body, format="json")


@pytest.mark.django_db
def test_an_owner_can_add_a_pet_with_photos_and_read_it_back():
    owner = person()
    res = _add(owner)
    assert res.status_code == 201, res.content
    pet = Pet.objects.get(pk=res.json()["pet_id"])
    assert pet.owner_account_id == owner.pk and pet.name == "Mochi" and pet.medical_conditions
    assert dict(pet.photos.values_list("url", "is_primary")) == {own_photo(owner, "m1"): False,
                                                                 own_photo(owner, "m2"): True}
    detail = c(owner).get(f"/api/v1/me/pets/{pet.pk}").json()
    assert detail["name"] == "Mochi" and detail["breed"] == "Puspin" and detail["sex"] == "female"
    assert detail["birthdate"] == "2022-03-01" and detail["photos"][0] == own_photo(owner, "m2")
    [row] = c(owner).get("/api/v1/me/pets").json()["results"]
    assert row["photo_url"] == own_photo(owner, "m2")


@pytest.mark.django_db
def test_adding_needs_a_name_and_a_species_and_a_signed_in_user():
    owner = person()
    assert c(owner).post("/api/v1/me/pets", {"name": "x"}, format="json").status_code == 400
    from rest_framework.test import APIClient
    assert APIClient().post("/api/v1/me/pets", BODY, format="json").status_code == 401


@pytest.mark.django_db
def test_a_pet_is_private_to_its_owner():
    owner, other = person(), person()
    pid = _add(owner).json()["pet_id"]
    assert c(other).get(f"/api/v1/me/pets/{pid}").status_code == 404
    assert c(other).get("/api/v1/me/pets").json()["results"] == []


@pytest.mark.django_db
def test_listing_one_of_my_pets_links_it_and_a_pet_has_one_live_listing_at_a_time():
    owner = person(member=True)
    pid = _add(owner).json()["pet_id"]
    body = {"pet": {"name": "Mochi", "species": "cat"}, "adoption_fee": "0", "city": "Marikina",
            "pet_id": pid}
    first = c(owner).post("/api/v1/listings", body, format="json")
    assert first.status_code == 201, first.content
    assert str(AdoptionListing.objects.get(pk=first.json()["listing_id"]).rehome_pet_id) == pid
    again = c(owner).post("/api/v1/listings", body, format="json")
    assert again.status_code == 409 and again.json()["error"]["code"] == "pet_already_listed"
    # withdrawing frees the pet to be listed again
    AdoptionListing.objects.filter(pk=first.json()["listing_id"]).update(status=ListingStatus.WITHDRAWN)
    assert c(owner).post("/api/v1/listings", body, format="json").status_code == 201


@pytest.mark.django_db
def test_you_cannot_list_someone_elses_pet_or_one_that_does_not_exist():
    owner, thief = person(), person(member=True)
    pid = _add(owner).json()["pet_id"]
    body = {"pet": {"name": "Mochi", "species": "cat"}, "adoption_fee": "0", "city": "Marikina"}
    assert c(thief).post("/api/v1/listings", {**body, "pet_id": pid}, format="json").status_code == 404
    assert c(thief).post("/api/v1/listings", {**body, "pet_id": "00000000-0000-0000-0000-000000000001"},
                         format="json").status_code == 404


@pytest.mark.django_db
def test_complete_transfers_the_pet_and_keeps_its_history_and_medical_notes():
    owner, adopter = person(member=True), person(member=True)
    pid = _add(owner).json()["pet_id"]
    lid = c(owner).post("/api/v1/listings", {"pet": {"name": "Mochi", "species": "cat"},
                        "adoption_fee": "0", "city": "Marikina", "pet_id": pid},
                        format="json").json()["listing_id"]
    c(owner).post(f"/api/v1/listings/{lid}/publish")
    inq = inquire(adopter, AdoptionListing.objects.get(pk=lid))
    post(owner, inq, "screen"); post(owner, inq, "reserve")
    res = post(owner, inq, "complete")
    assert res.status_code == 200 and res.json()["pet_id"] == pid      # the same Pet, not a copy
    pet = Pet.objects.get(pk=pid)
    assert pet.owner_account_id == adopter.pk and pet.medical_conditions == "Mild asthma"
    assert Pet.objects.count() == 1
    [h] = PetOwnershipHistory.objects.filter(pet=pet)
    assert (h.from_account_id, h.to_account_id) == (owner.pk, adopter.pk)
    assert str(h.listing_id) == lid
    assert c(owner).get(f"/api/v1/me/pets/{pid}").status_code == 404
    assert [p["pet_id"] for p in c(adopter).get("/api/v1/me/pets").json()["results"]] == [pid]


@pytest.mark.django_db
def test_complete_on_a_listing_with_no_linked_pet_still_creates_one():
    owner, adopter = person(member=True), person(member=True)
    item = listing(owner)
    item.photos.create(url="https://x/p", is_primary=True)
    inq = inquire(adopter, item)
    post(owner, inq, "screen"); post(owner, inq, "reserve")
    pid = post(owner, inq, "complete").json()["pet_id"]
    assert Pet.objects.get(pk=pid).owner_account_id == adopter.pk
    assert PetOwnershipHistory.objects.count() == 0
