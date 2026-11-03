"""Phase 4 of the My Pets review (docs/superpowers/specs/2026-10-28-my-pets-review.md) · the rows
`dev/test-plan-my-pets.md` Run 1 checked with a throwaway probe, kept as regression tests (NT-3),
the two races only one side of which had a test (NT-6), and two small edges (NT-9). Tests only:
each one pins behaviour that is already built. Row IDs are the test plan's."""
import threading
from unittest import mock

import pytest
from django.db import OperationalError, connection

from listings.models import AdoptionInquiry, AdoptionListing, InquiryStatus, ListingStatus
from listings.tests.adoption_helpers import c, inquire, listing, person, post
from pets.models import Pet, PetPhoto


def _photo(owner, name):
    return f"https://bkt.example/listing_photo/{owner.pk}/{name}"


def _create(owner, **body):
    return c(owner).post("/api/v1/me/pets", {"name": "Mochi", "species": "cat", **body}, format="json")


def _list(owner, pid):
    return c(owner).post("/api/v1/listings", {"pet": {"name": "Mochi", "species": "cat"}, "adoption_fee": "0",
                                              "city": "Marikina", "pet_id": pid}, format="json")


def _reserved(poster, adopter, pid):
    """A published rehoming listing of `pid`, reserved for `adopter`: one step from Complete."""
    lid = _list(poster, pid).json()["listing_id"]
    assert c(poster).post(f"/api/v1/listings/{lid}/publish").status_code == 200
    inq = inquire(adopter, AdoptionListing.objects.get(pk=lid))
    post(poster, inq, "screen"); post(poster, inq, "reserve")
    return lid, inq


def _refused_on(res, field):
    assert res.status_code == 400, res.content
    assert field in res.json()["error"]["details"], res.json()


# ── Validation (U2, U3, U4, U5, U6, U8, U9) ─────────────────────────────────────────────────

@pytest.mark.django_db
def test_u2_a_name_of_exactly_80_characters_is_stored_exactly():
    owner = person()
    name = "Ñiño" + "x" * 76
    res = _create(owner, name=name)
    assert res.status_code == 201, res.content
    assert Pet.objects.get(pk=res.json()["pet_id"]).name == name


@pytest.mark.django_db
@pytest.mark.parametrize("species", ["Dog", "bird", None])
def test_u3_a_species_that_is_not_dog_or_cat_is_refused(species):
    owner = person()
    _refused_on(_create(owner, species=species), "species")
    assert not Pet.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("birthdate", ["2022-02-30", "03/01/2022"])
def test_u4_a_birthdate_in_another_format_or_impossible_is_refused(birthdate):
    _refused_on(_create(person(), birthdate=birthdate), "birthdate")
    assert not Pet.objects.exists()


@pytest.mark.django_db
def test_u5_no_sex_is_stored_unknown_and_a_blank_one_is_refused():
    owner = person()
    pid = _create(owner).json()["pet_id"]
    assert Pet.objects.get(pk=pid).sex == "unknown"
    assert c(owner).get(f"/api/v1/me/pets/{pid}").json()["sex"] is None     # PP16 · read as null
    _refused_on(_create(owner, sex=""), "sex")
    assert Pet.objects.count() == 1


@pytest.mark.django_db
def test_u6_seven_photos_are_refused():
    owner = person()
    _refused_on(_create(owner, photos=[{"file_url": _photo(owner, str(i))} for i in range(7)]), "photos")
    assert not Pet.objects.exists() and not PetPhoto.objects.exists()


@pytest.mark.django_db
def test_u6_with_two_flagged_primary_only_the_first_flagged_is():
    owner = person()
    pid = _create(owner, photos=[{"file_url": _photo(owner, "a")},
                                 {"file_url": _photo(owner, "b"), "is_primary": True},
                                 {"file_url": _photo(owner, "c"), "is_primary": True}]).json()["pet_id"]
    primaries = PetPhoto.objects.filter(pet_id=pid, is_primary=True).values_list("url", flat=True)
    assert list(primaries) == [_photo(owner, "b")]


@pytest.mark.django_db
def test_u6_nt9_with_none_flagged_the_first_is_primary():
    owner = person()
    pid = _create(owner, photos=[{"file_url": _photo(owner, "a")},
                                 {"file_url": _photo(owner, "b")}]).json()["pet_id"]
    primaries = PetPhoto.objects.filter(pet_id=pid, is_primary=True).values_list("url", flat=True)
    assert list(primaries) == [_photo(owner, "a")]
    assert c(owner).get(f"/api/v1/me/pets/{pid}").json()["photos"][0] == _photo(owner, "a")


@pytest.mark.django_db
def test_u8_an_owner_id_or_date_in_the_body_is_ignored():
    owner, stranger = person(), person()
    fake_id = "00000000-0000-0000-0000-000000000001"
    res = _create(owner, owner_account=str(stranger.pk), pet_id=fake_id, created_at="2001-01-01T00:00:00Z")
    assert res.status_code == 201, res.content
    pet = Pet.objects.get(pk=res.json()["pet_id"])
    assert pet.owner_account_id == owner.pk
    assert str(pet.pk) != fake_id
    assert pet.created_at.year > 2001


@pytest.mark.django_db
@pytest.mark.parametrize(("sent", "stored"), [(True, True), (False, False), (None, None), ("yes", True)])
def test_u9_spayed_neutered_takes_true_false_null_and_drf_reads_yes_as_true(sent, stored):
    # "yes" → True is DRF's BooleanField, not a rule of ours (the review guessed a 400; harmless).
    owner = person()
    res = _create(owner, spayed_neutered=sent)
    assert res.status_code == 201, res.content
    assert Pet.objects.get(pk=res.json()["pet_id"]).spayed_neutered is stored


# ── Integration (I3, I4, I6) ────────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_i3_a_database_error_on_the_second_photo_leaves_no_pet_no_photo_and_no_event(
        django_capture_on_commit_callbacks):
    owner = person()
    real_create, calls = PetPhoto.objects.create, []

    def flaky(**kw):
        calls.append(kw)
        if len(calls) == 2:
            raise OperationalError("server closed the connection unexpectedly")
        return real_create(**kw)

    client = c(owner)
    client.raise_request_exception = False
    with mock.patch.object(PetPhoto.objects, "create", side_effect=flaky), \
         mock.patch("pets.service.emit") as emit, django_capture_on_commit_callbacks(execute=True):
        res = client.post("/api/v1/me/pets", {"name": "Mochi", "species": "cat",
                                              "photos": [{"file_url": _photo(owner, "a")},
                                                         {"file_url": _photo(owner, "b")}]}, format="json")
    assert res.status_code == 500
    assert len(calls) == 2
    assert not Pet.objects.exists() and not PetPhoto.objects.exists()
    emit.assert_not_called()                     # pet_added fires on commit, and nothing committed


@pytest.mark.django_db
def test_i4_a_database_error_inside_a_transfer_leaves_the_adoption_where_it_was():
    from pets.models import PetOwnershipHistory
    poster, adopter = person(member=True), person(member=True)
    pid = _create(poster).json()["pet_id"]
    lid, inq = _reserved(poster, adopter, pid)

    client = c(poster)
    client.raise_request_exception = False
    with mock.patch.object(PetOwnershipHistory.objects, "create",
                           side_effect=OperationalError("server closed the connection unexpectedly")), \
         mock.patch("listings.adoption_views.emit") as emit:
        res = client.post(f"/api/v1/inquiries/{inq.pk}/complete", {}, format="json")
    assert res.status_code == 500
    assert AdoptionInquiry.objects.get(pk=inq.pk).status == InquiryStatus.ACTIVE
    assert AdoptionListing.objects.get(pk=lid).status == ListingStatus.PENDING
    assert Pet.objects.get(pk=pid).owner_account_id == poster.pk
    assert not PetOwnershipHistory.objects.exists()
    assert "adoption_completed" not in [call.args[0] for call in emit.call_args_list]


@pytest.mark.django_db
def test_i6_an_old_client_sending_photos_as_bare_urls_gets_a_400_not_a_500():
    owner = person()
    _refused_on(_create(owner, photos=[_photo(owner, "a")]), "photos")
    assert not Pet.objects.exists()


# ── Failure (F5, F10) ───────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
@pytest.mark.parametrize(("field", "value"), [
    pytest.param("name", "Mo\x00chi", id="nul"),
    pytest.param("name", {"first": "Mochi"}, id="nested-object"),
    pytest.param("breed", ["Puspin"], id="nested-list"),
])
def test_f5_a_hostile_value_is_refused_on_its_field_and_writes_nothing(field, value):
    _refused_on(_create(person(), **{field: value}), field)
    assert not Pet.objects.exists()


@pytest.mark.django_db
def test_f5_five_megabytes_of_notes_never_reach_the_serializer():
    # Django's DATA_UPLOAD_MAX_MEMORY_SIZE refuses the body first (a plain 400, before DRF), so
    # the 1000-character cap (test_my_pets_profile_fields) is never even consulted.
    res = _create(person(), medical_conditions="x" * (5 * 1024 * 1024))
    assert res.status_code == 400
    assert not Pet.objects.exists()


@pytest.mark.django_db
def test_f10_a_moderated_listing_shows_on_the_card_and_the_owner_can_withdraw_and_relist():
    from django.utils import timezone
    owner = person(member=True)
    pid = _create(owner).json()["pet_id"]
    lid = _list(owner, pid).json()["listing_id"]
    assert c(owner).post(f"/api/v1/listings/{lid}/publish").status_code == 200
    AdoptionListing.objects.filter(pk=lid).update(hidden_at=timezone.now())

    [row] = c(owner).get("/api/v1/me/pets").json()["results"]
    assert row["open_listing"] == {"listing_id": lid, "status": "available", "removed": True}
    assert c(owner).post(f"/api/v1/listings/{lid}/withdraw", {}, format="json").status_code == 200
    assert c(owner).get("/api/v1/me/pets").json()["results"][0]["open_listing"] is None
    assert _list(owner, pid).status_code == 201


# ── Races (F1, F2) · NT-6 ───────────────────────────────────────────────────────────────────

def _race(*calls):
    """Run each (name, fn) on its own thread and connection at once; {name: status code}."""
    results, start = {}, threading.Barrier(len(calls))

    def run(name, fn):
        try:
            start.wait()
            results[name] = fn().status_code
        finally:
            connection.close()

    threads = [threading.Thread(target=run, args=call) for call in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


@pytest.mark.django_db(transaction=True)
def test_f1_two_listings_of_one_pet_at_once_one_wins_and_the_loser_writes_nothing():
    owner = person(member=True)
    pid = _create(owner).json()["pet_id"]

    results = _race(("a", lambda: _list(owner, pid)), ("b", lambda: _list(owner, pid)))

    assert sorted(results.values()) == [201, 409]
    assert AdoptionListing.objects.filter(rehome_pet_id=pid).count() == 1
    assert AdoptionListing.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_f2_a_remove_racing_a_complete_never_archives_the_adopters_pet():
    """The Complete locks the listing then the pet; the remove locks the pet and reads the listing.
    Whichever goes first, the pet ends up the adopter's and live, and nothing deadlocks: the remove
    sees a pending listing (409) or a pet that is no longer the caller's (404)."""
    poster, adopter = person(member=True), person(member=True)
    pid = _create(poster).json()["pet_id"]
    _, inq = _reserved(poster, adopter, pid)

    results = _race(("complete", lambda: post(poster, inq, "complete")),
                    ("remove", lambda: c(poster).delete(f"/api/v1/me/pets/{pid}", {}, format="json")))

    assert results["complete"] == 200
    assert results["remove"] in (404, 409)
    pet = Pet.objects.get(pk=pid)
    assert pet.owner_account_id == adopter.pk
    assert pet.removed_at is None


# ── Small edges · NT-9 ──────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_u13_a_rescue_listing_with_no_name_gives_the_adopter_a_pet_called_pet():
    poster, adopter = person(member=True), person(member=True)
    item = listing(poster, name="", status=ListingStatus.AVAILABLE)
    inq = inquire(adopter, item)
    post(poster, inq, "screen"); post(poster, inq, "reserve")
    res = post(poster, inq, "complete")
    assert res.status_code == 200, res.content
    assert Pet.objects.get(pk=res.json()["pet_id"]).name == "Pet"
