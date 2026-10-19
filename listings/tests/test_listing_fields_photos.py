"""AD11 (the fields the detail shows can now be written) and AD9 (photos in create and PATCH)."""
import pytest

from listings.models import AdoptionListing
from listings.tests.adoption_helpers import c, listing, person

BODY = {"pet": {"name": "Bantay", "species": "dog", "size_category": "medium",
                "spayed_neutered": True, "vaccinated": True, "walkable": True,
                "temperament": "Calm, good with kids"},
        "requirements": "Fenced yard. No chaining.", "adoption_fee": "0", "city": "Marikina"}


@pytest.mark.django_db
def test_create_writes_the_health_and_requirements_fields_and_detail_reads_them_back():
    poster = person(member=True)
    lid = c(poster).post("/api/v1/listings", BODY, format="json").json()["listing_id"]
    row = AdoptionListing.objects.get(pk=lid)
    assert (row.size_category, row.spayed_neutered, row.vaccinated, row.walkable) == (
        "medium", True, True, True)
    assert row.temperament == "Calm, good with kids" and row.requirements == "Fenced yard. No chaining."
    body = c(poster).get(f"/api/v1/listings/{lid}").json()
    assert body["requirements"] == "Fenced yard. No chaining."
    assert body["pet"]["vaccinated"] is True and body["pet"]["size_category"] == "medium"


@pytest.mark.django_db
def test_patch_edits_those_fields_and_only_those_it_names():
    poster = person(member=True)
    item = listing(poster, vaccinated=True, requirements="old")
    res = c(poster).patch(f"/api/v1/listings/{item.pk}",
                          {"requirements": "new", "spayed_neutered": False, "walkable": True,
                           "size_category": "large", "temperament": "Shy"}, format="json")
    assert res.status_code == 200, res.content
    item.refresh_from_db()
    assert (item.requirements, item.spayed_neutered, item.walkable, item.size_category,
            item.temperament, item.vaccinated) == ("new", False, True, "large", "Shy", True)
    assert res.json()["pet"]["spayed_neutered"] is False and res.json()["requirements"] == "new"


@pytest.mark.django_db
def test_an_unknown_size_is_rejected():
    poster = person(member=True)
    bad = {**BODY, "pet": {**BODY["pet"], "size_category": "huge"}}
    assert c(poster).post("/api/v1/listings", bad, format="json").status_code == 400


# ── AD9 · photos ────────────────────────────────────────────────────────────────
def _urls(item):
    return list(item.photos.order_by("-is_primary").values_list("url", "is_primary"))


@pytest.mark.django_db
def test_create_marks_the_first_photo_primary_unless_one_is_named():
    poster = person(member=True)
    body = {**BODY, "photos": [{"file_url": "https://x/1"}, {"file_url": "https://x/2"}]}
    item = AdoptionListing.objects.get(pk=c(poster).post("/api/v1/listings", body, format="json"
                                                         ).json()["listing_id"])
    assert dict(_urls(item)) == {"https://x/1": True, "https://x/2": False}
    body2 = {**BODY, "photos": [{"file_url": "https://x/1"},
                                {"file_url": "https://x/2", "is_primary": True}]}
    item2 = AdoptionListing.objects.get(pk=c(poster).post("/api/v1/listings", body2, format="json"
                                                          ).json()["listing_id"])
    assert dict(_urls(item2)) == {"https://x/1": False, "https://x/2": True}


@pytest.mark.django_db
def test_patch_replaces_the_photo_set_and_detail_lists_the_primary_first():
    poster = person(member=True)
    item = listing(poster)
    item.photos.create(url="https://x/old", is_primary=True)
    res = c(poster).patch(f"/api/v1/listings/{item.pk}",
                          {"photos": [{"file_url": "https://x/a"},
                                      {"file_url": "https://x/b", "is_primary": True}]},
                          format="json")
    assert res.status_code == 200, res.content
    assert dict(_urls(item)) == {"https://x/a": False, "https://x/b": True}
    assert c(poster).get(f"/api/v1/listings/{item.pk}").json()["photos"][0] == "https://x/b"
    assert res.json()["photos"] == ["https://x/b", "https://x/a"]


@pytest.mark.django_db
def test_patch_without_photos_leaves_them_and_an_empty_list_clears_them():
    poster = person(member=True)
    item = listing(poster)
    item.photos.create(url="https://x/keep", is_primary=True)
    c(poster).patch(f"/api/v1/listings/{item.pk}", {"city": "Pasig"}, format="json")
    assert item.photos.count() == 1
    c(poster).patch(f"/api/v1/listings/{item.pk}", {"photos": []}, format="json")
    assert item.photos.count() == 0


@pytest.mark.django_db
def test_more_than_six_photos_is_refused_on_create_and_patch():
    poster = person(member=True)
    seven = [{"file_url": f"https://x/{i}"} for i in range(7)]
    assert c(poster).post("/api/v1/listings", {**BODY, "photos": seven}, format="json"
                          ).status_code == 400
    item = listing(poster)
    assert c(poster).patch(f"/api/v1/listings/{item.pk}", {"photos": seven}, format="json"
                           ).status_code == 400
