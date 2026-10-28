"""Step 5 of the My Pets review (docs/superpowers/specs/2026-10-28-my-pets-review.md) · an owner
manages their pets: PATCH and DELETE /me/pets/{id} (PP12, archive per PQ1), a shelter can't add
one (PQ3), and the previous owner keeps a read-only "Rehomed" trace frozen at the transfer (PQ2)."""
import threading

import pytest
from django.db import connection

from accounts.factories import AccountFactory
from listings.models import AdoptionListing, ListingStatus, Pet, PetOwnershipHistory, PetRemovedReason
from listings.tests.adoption_helpers import c, inquire, person, post

PET = {"name": "Mochi", "species": "cat", "breed": "Puspin", "sex": "female", "birthdate": "2022-03-01",
       "temperament": "Lap cat", "medical_conditions": "Mild asthma", "vet_contact": "Dr Reyes"}


def _photo(owner, name):
    return f"https://bkt.example/listing_photo/{owner.pk}/{name}"


def _add(owner, **extra):
    res = c(owner).post("/api/v1/me/pets", {**PET, **extra}, format="json")
    assert res.status_code == 201, res.content
    return res.json()["pet_id"]


def _patch(owner, pid, body):
    return c(owner).patch(f"/api/v1/me/pets/{pid}", body, format="json")


def _delete(owner, pid, body=None):
    return c(owner).delete(f"/api/v1/me/pets/{pid}", body or {}, format="json")


def _list(owner, pid):
    res = c(owner).post("/api/v1/listings", {"pet": {"name": "Mochi", "species": "cat"},
                                             "adoption_fee": "0", "city": "Marikina", "pet_id": pid},
                        format="json")
    assert res.status_code == 201, res.content
    return res.json()["listing_id"]


def _rehome(poster, adopter, pid):
    lid = _list(poster, pid)
    c(poster).post(f"/api/v1/listings/{lid}/publish")
    inq = inquire(adopter, AdoptionListing.objects.get(pk=lid))
    post(poster, inq, "screen"); post(poster, inq, "reserve")
    assert post(poster, inq, "complete").status_code == 200


# ── PATCH ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_an_owner_edits_only_the_fields_they_send():
    owner = person()
    pid = _add(owner)
    res = _patch(owner, pid, {"vet_contact": "Dr Santos", "spayed_neutered": True, "birthdate": None})
    assert res.status_code == 200, res.content
    d = c(owner).get(f"/api/v1/me/pets/{pid}").json()
    assert (d["vet_contact"], d["spayed_neutered"], d["birthdate"]) == ("Dr Santos", True, None)
    assert (d["name"], d["breed"], d["medical_conditions"]) == ("Mochi", "Puspin", "Mild asthma")


@pytest.mark.django_db
def test_photos_present_replace_the_set_and_absent_leave_it():
    owner = person()
    pid = _add(owner, photos=[{"file_url": _photo(owner, "a")}])
    _patch(owner, pid, {"name": "Mochi II"})
    assert c(owner).get(f"/api/v1/me/pets/{pid}").json()["photos"] == [_photo(owner, "a")]
    _patch(owner, pid, {"photos": [{"file_url": _photo(owner, "b")},
                                   {"file_url": _photo(owner, "a"), "is_primary": True}]})
    assert c(owner).get(f"/api/v1/me/pets/{pid}").json()["photos"] == [_photo(owner, "a"), _photo(owner, "b")]
    _patch(owner, pid, {"photos": []})
    assert c(owner).get(f"/api/v1/me/pets/{pid}").json()["photos"] == []


@pytest.mark.django_db
def test_an_adopted_pet_keeps_its_previous_owners_photos_on_edit_but_takes_no_new_foreign_one():
    a, b, stranger = person(member=True), person(member=True), person()
    pid = _add(a, photos=[{"file_url": _photo(a, "face")}])
    _rehome(a, b, pid)
    kept = _patch(b, pid, {"photos": [{"file_url": _photo(a, "face")}, {"file_url": _photo(b, "new")}]})
    assert kept.status_code == 200, kept.content
    foreign = _patch(b, pid, {"photos": [{"file_url": _photo(stranger, "x")}]})
    assert foreign.status_code == 400
    assert c(b).get(f"/api/v1/me/pets/{pid}").json()["photos"] == [_photo(a, "face"), _photo(b, "new")]


@pytest.mark.django_db
@pytest.mark.parametrize("body", [{"name": ""}, {"name": "x" * 81}, {"species": "bird"}, {"sex": ""},
                                  {"birthdate": "2022-02-30"}, {"photos": [{"file_url": "https://evil/x"}]}])
def test_a_bad_edit_is_refused_and_changes_nothing(body):
    owner = person()
    pid = _add(owner)
    before = Pet.objects.get(pk=pid).updated_at
    assert _patch(owner, pid, body).status_code == 400
    assert Pet.objects.get(pk=pid).updated_at == before


@pytest.mark.django_db
def test_the_species_is_locked_while_a_listing_describes_the_animal():
    owner = person(member=True)
    pid = _add(owner)
    lid = _list(owner, pid)
    res = _patch(owner, pid, {"species": "dog"})
    assert res.status_code == 409 and res.json()["error"]["code"] == "pet_listed"
    assert _patch(owner, pid, {"species": "cat", "temperament": "Calmer now"}).status_code == 200
    AdoptionListing.objects.filter(pk=lid).update(status=ListingStatus.WITHDRAWN)
    assert _patch(owner, pid, {"species": "dog"}).status_code == 200


@pytest.mark.django_db
def test_nobody_else_can_edit_or_remove_a_pet():
    owner, other = person(), person()
    pid = _add(owner)
    assert _patch(other, pid, {"name": "Mine"}).status_code == 404
    assert _delete(other, pid).status_code == 404
    from rest_framework.test import APIClient
    assert APIClient().patch(f"/api/v1/me/pets/{pid}", {"name": "x"}, format="json").status_code == 401
    assert Pet.objects.get(pk=pid).name == "Mochi" and Pet.objects.get(pk=pid).removed_at is None


# ── DELETE ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
@pytest.mark.parametrize("reason", ["passed_away", "given_away", "other"])
def test_removing_a_pet_archives_it_with_the_reason(reason):
    owner = person()
    pid = _add(owner)
    assert _delete(owner, pid, {"reason": reason}).status_code == 204
    pet = Pet.objects.get(pk=pid)
    assert pet.removed_at is not None and pet.removed_reason == reason
    assert c(owner).get("/api/v1/me/pets").json()["results"] == []
    assert c(owner).get(f"/api/v1/me/pets/{pid}").status_code == 404
    assert _delete(owner, pid).status_code == 404        # already gone
    assert _patch(owner, pid, {"name": "x"}).status_code == 404


@pytest.mark.django_db
def test_removing_defaults_to_other_and_refuses_a_system_reason():
    owner = person()
    pid = _add(owner)
    assert _delete(owner, pid, {"reason": "rehomed_outside"}).status_code == 400
    assert _delete(owner, pid).status_code == 204
    assert Pet.objects.get(pk=pid).removed_reason == PetRemovedReason.OTHER


@pytest.mark.django_db
def test_a_listed_pet_cannot_be_removed():
    owner = person(member=True)
    pid = _add(owner)
    lid = _list(owner, pid)
    res = _delete(owner, pid)
    assert res.status_code == 409 and res.json()["error"]["details"]["listing_id"] == lid
    detail = c(owner).get(f"/api/v1/me/pets/{pid}").json()
    assert detail["open_listing"] == {"listing_id": lid, "status": "draft", "removed": False}


@pytest.mark.django_db(transaction=True)
def test_a_remove_racing_a_listing_never_archives_a_listed_pet():
    """F2-shaped: both writers lock the pet row, so exactly one wins and the other sees it."""
    owner = person(member=True)
    pid = _add(owner)
    results = {}

    def run(name, fn):
        try:
            results[name] = fn().status_code
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=("list", lambda: c(owner).post(
                   "/api/v1/listings", {"pet": {"name": "Mochi", "species": "cat"}, "adoption_fee": "0",
                                        "city": "Marikina", "pet_id": pid}, format="json"))),
               threading.Thread(target=run, args=("delete", lambda: _delete(owner, pid)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    pet = Pet.objects.get(pk=pid)
    listed = AdoptionListing.objects.filter(rehome_pet=pet).exists()
    assert sorted(results.values()) in ([201, 409], [204, 404])
    assert listed != (pet.removed_at is not None)


# ── PQ3 ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_a_shelter_cannot_add_a_pet():
    shelter = AccountFactory(account_type="shelter")
    res = c(shelter).post("/api/v1/me/pets", PET, format="json")
    assert res.status_code == 403 and res.json()["error"]["code"] == "not_for_shelters"
    assert not Pet.objects.exists()


# ── PQ2 ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_the_previous_owner_sees_a_trace_frozen_at_the_transfer():
    a, b = person(member=True), person(member=True)
    pid = _add(a, photos=[{"file_url": _photo(a, "face")}])
    _rehome(a, b, pid)
    _patch(b, pid, {"name": "Bagong Pangalan", "photos": [{"file_url": _photo(b, "new")}]})

    body = c(a).get("/api/v1/me/pets").json()
    assert body["results"] == []
    [trace] = body["rehomed"]
    assert (trace["name"], trace["photo_url"]) == ("Mochi", _photo(a, "face"))
    assert trace["rehomed_at"]
    assert set(trace) == {"name", "photo_url", "rehomed_at"}     # no pet_id: nothing to open
    assert c(b).get("/api/v1/me/pets").json()["rehomed"] == []
    assert c(a).get(f"/api/v1/me/pets/{pid}").status_code == 404


@pytest.mark.django_db
def test_a_pet_with_no_photo_leaves_a_trace_with_a_null_photo():
    a, b = person(member=True), person(member=True)
    pid = _add(a)
    _rehome(a, b, pid)
    [trace] = c(a).get("/api/v1/me/pets").json()["rehomed"]
    assert trace["photo_url"] is None
    assert PetOwnershipHistory.objects.get(pet_id=pid).pet_name == "Mochi"


@pytest.mark.django_db
def test_the_migration_snapshots_transfers_recorded_before_the_columns_existed():
    import importlib

    from django.apps import apps

    from listings.models import PetPhoto
    a, b = person(), person()
    pet = Pet.objects.create(owner_account=b, name="Bruno", species="dog")
    PetPhoto.objects.create(pet=pet, url="https://bkt/side.jpg")
    PetPhoto.objects.create(pet=pet, url="https://bkt/face.jpg", is_primary=True)
    old = PetOwnershipHistory.objects.create(pet=pet, from_account=a, to_account=b)      # pre-0012 row
    done = PetOwnershipHistory.objects.create(pet=pet, from_account=b, to_account=a, pet_name="Kept")

    migration = importlib.import_module("listings.migrations.0012_pet_manage_rehomed_trace")
    migration.snapshot_existing_transfers(apps, None)

    old.refresh_from_db(); done.refresh_from_db()
    assert (old.pet_name, old.photo_url) == ("Bruno", "https://bkt/face.jpg")
    assert done.pet_name == "Kept"                                   # an existing snapshot is left alone
