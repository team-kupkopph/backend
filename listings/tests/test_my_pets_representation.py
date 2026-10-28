"""Step 7 of the My Pets review (docs/superpowers/specs/2026-10-28-my-pets-review.md) · one way a
pet reads (PP6): the list, the detail and the data export all build on `pet_fields`, and the
export carries the pet's photos and changes of hands (PP18) and the pets the caller rehomed (PQ2)."""
import json
from datetime import date
from decimal import Decimal

import pytest

from listings.models import AdoptionListing, Pet, PetPhoto
from listings.representations import pet_fields
from listings.tests.adoption_helpers import c, inquire, person, post

#: Pet columns that are not the animal's description, and the one whose key differs on the way out.
NOT_EXPORTED_AS_FIELDS = {"owner_account"}          # the export IS the owner's; no self-reference
RENAMED = {"date_of_birth": "birthdate"}


def _full_pet(owner):
    pet = Pet.objects.create(
        owner_account=owner, name="Mochi", species="cat", breed="Puspin", sex="female",
        date_of_birth=date(2022, 3, 1), dob_approximate=True, color_markings="calico",
        weight_kg=Decimal("4.20"), size_category="small", spayed_neutered=True, microchip_no="985112",
        feeding_routine="twice a day", allergies="chicken", medications="none", medical_conditions="asthma",
        temperament="lap cat", vet_contact="Dr Reyes", emergency_contact="Ana 0918")
    PetPhoto.objects.create(pet=pet, url="https://bkt/side.jpg")
    PetPhoto.objects.create(pet=pet, url="https://bkt/face.jpg", is_primary=True)
    return pet


def _export(who):
    res = c(who).get("/api/v1/me/export")
    assert res.status_code == 200, res.content
    return res.json()


def _rehome(poster, adopter, pet):
    res = c(poster).post("/api/v1/listings", {"pet": {"name": pet.name, "species": pet.species},
                                              "adoption_fee": "0", "city": "Marikina", "pet_id": str(pet.pk)},
                         format="json")
    lid = res.json()["listing_id"]
    c(poster).post(f"/api/v1/listings/{lid}/publish")
    inq = inquire(adopter, AdoptionListing.objects.get(pk=lid))
    post(poster, inq, "screen"); post(poster, inq, "reserve")
    assert post(poster, inq, "complete").status_code == 200
    return lid


@pytest.mark.django_db
def test_the_scan_finds_the_pet_columns():
    # Without this, a renamed model would make the guard below check an empty list and pass.
    names = {f.name for f in Pet._meta.concrete_fields}
    assert {"name", "species", "weight_kg", "microchip_no", "emergency_contact", "removed_at"} <= names


@pytest.mark.django_db
def test_every_pet_column_reaches_the_export():
    """The guard PP6 asked for: a column added to `pet` without being exported fails here."""
    owner = person()
    _full_pet(owner)
    [row] = _export(owner)["pets"]
    for f in Pet._meta.concrete_fields:
        if f.name in NOT_EXPORTED_AS_FIELDS:
            continue
        key = RENAMED.get(f.name, f.name)
        assert key in row, f"pet.{f.name} is not in the export"


@pytest.mark.django_db
def test_the_detail_and_the_export_read_a_pet_the_same_way():
    owner = person()
    pet = _full_pet(owner)
    detail = c(owner).get(f"/api/v1/me/pets/{pet.pk}").json()
    [row] = _export(owner)["pets"]
    shared = pet_fields(Pet.objects.prefetch_related("photos").get(pk=pet.pk))
    for key, value in shared.items():
        assert detail[key] == value == row[key], key
    assert row["photos"] == ["https://bkt/face.jpg", "https://bkt/side.jpg"]      # primary first
    assert (row["weight_kg"], row["birthdate"], row["dob_approximate"]) == ("4.20", "2022-03-01", True)
    [summary] = c(owner).get("/api/v1/me/pets").json()["results"]
    assert summary == {"pet_id": str(pet.pk), "name": "Mochi", "species": "cat",
                       "photo_url": "https://bkt/face.jpg",
                       "open_listing": None, "lost_report_id": None}      # PP14 · the card's state


@pytest.mark.django_db
def test_an_archived_pet_is_still_in_the_owners_export():
    owner = person()
    pet = _full_pet(owner)
    c(owner).delete(f"/api/v1/me/pets/{pet.pk}", {"reason": "passed_away"}, format="json")
    [row] = _export(owner)["pets"]
    assert row["removed_reason"] == "passed_away" and row["removed_at"]


@pytest.mark.django_db
def test_a_pets_changes_of_hands_are_exported_without_the_other_person():
    a, b = person(member=True), person(member=True)
    pet = _full_pet(a)
    lid = _rehome(a, b, pet)

    b_export = _export(b)
    [row] = b_export["pets"]
    assert row["ownership_history"] == [
        {"transferred_at": row["ownership_history"][0]["transferred_at"], "from_you": False, "to_you": True}]
    assert row["medical_conditions"] == "asthma"                 # the record travelled with the pet
    pets_text = json.dumps(b_export["pets"])
    assert str(a.pk) not in pets_text and a.email not in pets_text      # never the previous owner

    a_export = _export(a)
    assert a_export["pets"] == []
    [gone] = a_export["rehomed_pets"]
    assert gone == {"name": "Mochi", "photo_url": "https://bkt/face.jpg",
                    "rehomed_at": gone["rehomed_at"], "listing_id": lid}
    assert str(b.pk) not in json.dumps(a_export["rehomed_pets"])         # never the new owner


@pytest.mark.django_db
def test_the_rehomed_record_is_the_pet_as_it_left_not_as_it_is_now():
    a, b = person(member=True), person(member=True)
    pet = _full_pet(a)
    _rehome(a, b, pet)
    c(b).patch(f"/api/v1/me/pets/{pet.pk}", {"name": "Bagong Pangalan"}, format="json")
    assert _export(a)["rehomed_pets"][0]["name"] == "Mochi"
    assert _export(b)["rehomed_pets"] == []


@pytest.mark.django_db
def test_the_export_says_its_format():
    assert _export(person())["export_format"] == "kupkop.export.v3"
