"""dev review 2026-10-28 (docs/superpowers/specs/2026-10-28-my-pets-review.md) · the four Critical
fixes to My Pets: PP1 a rehomed pet can be rehomed again, PP2 a pet rehomed outside Kupkop is
archived (PQ1), PP3 a rehoming listing is the pet's own animal, PP4 a public adoption gives the
adopter the listing's description of the animal, and PP5 a pet photo is the owner's own upload."""
import pytest

from listings.models import AdoptionListing, ListingStatus, Pet, PetRemovedReason
from listings.tests.adoption_helpers import c, inquire, listing, person, post

PET = {"name": "Mochi", "species": "cat", "breed": "Puspin", "sex": "female",
       "birthdate": "2022-03-01", "size_category": "small", "spayed_neutered": True,
       "temperament": "Lap cat", "medical_conditions": "Mild asthma"}


def _photo(owner, name):
    return f"https://bkt.example/listing_photo/{owner.pk}/{name}"


def _add(owner, **extra):
    res = c(owner).post("/api/v1/me/pets", {**PET, **extra}, format="json")
    assert res.status_code == 201, res.content
    return res.json()["pet_id"]


def _list(owner, pid, **pet):
    return c(owner).post("/api/v1/listings", {"pet": {"name": "Mochi", "species": "cat", **pet},
                                              "adoption_fee": "0", "city": "Marikina", "pet_id": pid},
                         format="json")


def _complete(poster, adopter, lid):
    c(poster).post(f"/api/v1/listings/{lid}/publish")
    inq = inquire(adopter, AdoptionListing.objects.get(pk=lid))
    post(poster, inq, "screen"); post(poster, inq, "reserve")
    res = post(poster, inq, "complete")
    assert res.status_code == 200, res.content
    return res.json()["pet_id"]


# ── PP1 ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_the_new_owner_can_rehome_a_pet_they_were_rehomed():
    a, b, c3 = person(member=True), person(member=True), person(member=True)
    pid = _add(a)
    assert _complete(a, b, _list(a, pid).json()["listing_id"]) == pid
    again = _list(b, pid)
    assert again.status_code == 201, again.content
    assert _complete(b, c3, again.json()["listing_id"]) == pid
    assert Pet.objects.get(pk=pid).owner_account_id == c3.pk
    assert Pet.objects.get(pk=pid).ownership_history.count() == 2


@pytest.mark.django_db
@pytest.mark.parametrize("status", [ListingStatus.DRAFT, ListingStatus.AVAILABLE, ListingStatus.PENDING])
def test_an_open_listing_still_blocks_a_second(status):
    owner = person(member=True)
    pid = _add(owner)
    AdoptionListing.objects.filter(pk=_list(owner, pid).json()["listing_id"]).update(status=status)
    again = _list(owner, pid)
    assert again.status_code == 409 and again.json()["error"]["code"] == "pet_already_listed"


# ── PP2 ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_mark_adopted_archives_the_rehomed_pet_out_of_my_pets():
    owner = person(member=True)
    pid, keep = _add(owner), _add(owner, name="Bruno")
    lid = _list(owner, pid).json()["listing_id"]
    c(owner).post(f"/api/v1/listings/{lid}/publish")
    assert c(owner).post(f"/api/v1/listings/{lid}/mark-adopted", {}, format="json").status_code == 200

    pet = Pet.objects.get(pk=pid)          # kept, not deleted (PQ1)
    assert pet.removed_at is not None and pet.removed_reason == PetRemovedReason.REHOMED_OUTSIDE
    assert pet.owner_account_id == owner.pk
    assert [p["pet_id"] for p in c(owner).get("/api/v1/me/pets").json()["results"]] == [keep]
    assert c(owner).get(f"/api/v1/me/pets/{pid}").status_code == 404
    assert _list(owner, pid).status_code == 404          # an archived pet can't be listed again
    export = c(owner).get("/api/v1/me/export").json()
    archived = next(p for p in export["pets"] if p["pet_id"] == pid)
    assert archived["removed_reason"] == "rehomed_outside" and archived["removed_at"]


@pytest.mark.django_db
def test_mark_adopted_on_an_ordinary_listing_touches_no_pet():
    owner = person(member=True)
    pid = _add(owner)
    item = listing(owner)
    assert c(owner).post(f"/api/v1/listings/{item.pk}/mark-adopted", {}, format="json").status_code == 200
    assert Pet.objects.get(pk=pid).removed_at is None


@pytest.mark.django_db
def test_an_archived_pet_cannot_be_linked_to_a_lost_report():
    from sagip.models import StrayReport
    owner = person()
    pid = _add(owner)
    Pet.objects.filter(pk=pid).update(removed_at="2026-10-01T00:00:00Z")
    res = c(owner).post("/api/v1/reports", {"lat": 14.6507, "lng": 121.1029, "city": "Marikina",
                                            "report_type": "lost", "species": "cat", "pet_id": pid},
                        format="json")
    assert res.status_code == 201, res.content
    report = StrayReport.objects.get(pk=res.json()["report_id"])
    assert report.pet_id is None and report.breed is None      # nothing prefilled from it


# ── PP3 ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_a_rehoming_listing_cannot_call_the_pet_another_animal():
    owner = person(member=True)
    pid = _add(owner)
    res = _list(owner, pid, species="dog")
    assert res.status_code == 422 and res.json()["error"]["code"] == "pet_species_mismatch"
    assert res.json()["error"]["details"]["species"] == "cat"
    assert not AdoptionListing.objects.exists()


@pytest.mark.django_db
def test_blanks_on_a_rehoming_listing_come_from_the_pet():
    owner = person(member=True)
    pid = _add(owner, photos=[{"file_url": _photo(owner, "side")},
                              {"file_url": _photo(owner, "face"), "is_primary": True}])
    res = _list(owner, pid)            # only name + species sent, as a failed prefill would
    assert res.status_code == 201, res.content
    item = AdoptionListing.objects.get(pk=res.json()["listing_id"])
    assert (item.breed, item.sex, str(item.date_of_birth), item.size_category, item.spayed_neutered,
            item.temperament) == ("Puspin", "female", "2022-03-01", "small", True, "Lap cat")
    assert [p.url for p in item.photos.order_by("-is_primary")] == [_photo(owner, "face"),
                                                                     _photo(owner, "side")]


@pytest.mark.django_db
def test_what_the_owner_typed_on_the_listing_wins_over_the_pet():
    owner = person(member=True)
    pid = _add(owner)
    res = _list(owner, pid, breed="Siamese mix", temperament="Shy at first", spayed_neutered=False,
                photos=[])
    item = AdoptionListing.objects.get(pk=res.json()["listing_id"])
    assert (item.breed, item.temperament, item.spayed_neutered) == ("Siamese mix", "Shy at first", False)
    assert item.photos.count() == 0            # an explicit empty set is respected


@pytest.mark.django_db
def test_a_pet_of_unknown_sex_lists_with_a_blank_sex():
    owner = person(member=True)
    pid = _add(owner, sex="unknown")
    item = AdoptionListing.objects.get(pk=_list(owner, pid).json()["listing_id"])
    assert item.sex == ""


# ── PP4 ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_a_public_adoption_gives_the_adopter_the_listings_description():
    poster, adopter = person(member=True), person(member=True)
    res = c(poster).post("/api/v1/listings", {
        "pet": {"name": "Rex", "species": "dog", "breed": "Aspin", "sex": "male",
                "birthdate": "2021-01-01", "size_category": "medium", "spayed_neutered": True,
                "temperament": "Calm"},
        "adoption_fee": "0", "city": "Marikina"}, format="json")
    pid = _complete(poster, adopter, res.json()["listing_id"])
    d = c(adopter).get(f"/api/v1/me/pets/{pid}").json()
    assert (d["name"], d["species"], d["breed"], d["sex"], d["birthdate"], d["size_category"],
            d["spayed_neutered"], d["temperament"]) == (
        "Rex", "dog", "Aspin", "male", "2021-01-01", "medium", True, "Calm")


@pytest.mark.django_db
def test_a_listing_with_no_sex_gives_a_pet_of_unknown_sex():
    poster, adopter = person(member=True), person(member=True)
    item = listing(poster, sex="")
    inq = inquire(adopter, item)
    post(poster, inq, "screen"); post(poster, inq, "reserve")
    pid = post(poster, inq, "complete").json()["pet_id"]
    assert Pet.objects.get(pk=pid).sex == "unknown"


# ── PP5 ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
@pytest.mark.parametrize("url", [
    "https://tracker.example/pixel.gif",
    "https://bkt.example/report_photo/{me}/x.jpg",          # own upload, wrong purpose
    "https://bkt.example/listing_photo/{other}/x.jpg",      # someone else's upload
    "https://bkt.example/listing_photo/{me}/x.jpg?u=1",
    "javascript:alert(1)",
])
def test_a_pet_photo_must_be_the_owners_own_upload(url):
    owner, other = person(), person()
    res = c(owner).post("/api/v1/me/pets", {**PET, "photos": [
        {"file_url": url.format(me=owner.pk, other=other.pk)}]}, format="json")
    assert res.status_code == 400, res.content
    assert not Pet.objects.exists()
