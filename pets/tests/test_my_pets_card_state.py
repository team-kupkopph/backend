"""Step 9 of the My Pets review (docs/superpowers/specs/2026-10-28-my-pets-review.md, PP14) · a pet's
card and screen say what it is in the middle of: its open rehoming listing (and whether moderation
removed it) and its open lost report."""
import pytest
from django.contrib.gis.geos import Point
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from listings.models import AdoptionListing, ListingStatus
from listings.tests.adoption_helpers import c, person
from pets.models import Pet
from sagip.models import StrayReport, StrayStatus


def _pet(owner, name="Mochi"):
    return Pet.objects.create(owner_account=owner, name=name, species="cat")


def _listing(owner, pet, status=ListingStatus.DRAFT, **kw):
    return AdoptionListing.objects.create(posted_by=owner, rehome_pet=pet, name=pet.name, species="cat",
                                          city="Marikina", status=status, **kw)


def _lost(owner, pet, status=StrayStatus.REPORTED, report_type="lost"):
    return StrayReport.objects.create(reporter_account=owner, report_type=report_type, species="cat",
                                      condition="healthy", geom=Point(121.1, 14.65), pet=pet, status=status)


def _rows(owner):
    return {r["pet_id"]: r for r in c(owner).get("/api/v1/me/pets").json()["results"]}


@pytest.mark.django_db
def test_a_pet_with_nothing_going_on_says_so():
    owner = person()
    pet = _pet(owner)
    row = _rows(owner)[str(pet.pk)]
    assert row["open_listing"] is None and row["lost_report_id"] is None
    assert set(row) == {"pet_id", "name", "species", "birthdate", "photo_url", "open_listing", "lost_report_id"}
    assert row["birthdate"] is None                     # step 11 · no birthdate, no age on the card


@pytest.mark.django_db
@pytest.mark.parametrize("status", [ListingStatus.DRAFT, ListingStatus.AVAILABLE, ListingStatus.PENDING])
def test_an_open_listing_is_on_the_card(status):
    owner = person()
    pet = _pet(owner)
    item = _listing(owner, pet, status)
    assert _rows(owner)[str(pet.pk)]["open_listing"] == {"listing_id": str(item.pk), "status": status,
                                                         "removed": False}


@pytest.mark.django_db
@pytest.mark.parametrize("status", [ListingStatus.ADOPTED, ListingStatus.WITHDRAWN])
def test_a_closed_listing_is_not(status):
    owner = person()
    pet = _pet(owner)
    _listing(owner, pet, status)
    assert _rows(owner)[str(pet.pk)]["open_listing"] is None


@pytest.mark.django_db
def test_a_listing_moderation_removed_says_so():
    owner = person()
    pet = _pet(owner)
    _listing(owner, pet, ListingStatus.AVAILABLE, hidden_at=timezone.now())
    assert _rows(owner)[str(pet.pk)]["open_listing"]["removed"] is True


@pytest.mark.django_db
def test_an_open_lost_report_is_on_the_card_until_it_is_resolved():
    owner = person()
    pet = _pet(owner)
    report = _lost(owner, pet)
    assert _rows(owner)[str(pet.pk)]["lost_report_id"] == str(report.pk)
    StrayReport.objects.filter(pk=report.pk).update(status=StrayStatus.RESOLVED)
    assert _rows(owner)[str(pet.pk)]["lost_report_id"] is None


@pytest.mark.django_db
def test_only_a_lost_report_counts():
    owner = person()
    pet = _pet(owner)
    _lost(owner, pet, report_type="found")
    assert _rows(owner)[str(pet.pk)]["lost_report_id"] is None


@pytest.mark.django_db
def test_the_pet_screen_carries_the_same_state():
    owner = person()
    pet = _pet(owner)
    item = _listing(owner, pet)
    report = _lost(owner, pet)
    detail = c(owner).get(f"/api/v1/me/pets/{pet.pk}").json()
    assert detail["open_listing"] == {"listing_id": str(item.pk), "status": "draft", "removed": False}
    assert detail["lost_report_id"] == str(report.pk)


@pytest.mark.django_db
def test_each_pet_gets_its_own_state():
    owner = person()
    listed, lost, plain = _pet(owner, "A"), _pet(owner, "B"), _pet(owner, "C")
    _listing(owner, listed)
    _lost(owner, lost)
    rows = _rows(owner)
    assert rows[str(listed.pk)]["open_listing"] and not rows[str(listed.pk)]["lost_report_id"]
    assert rows[str(lost.pk)]["lost_report_id"] and not rows[str(lost.pk)]["open_listing"]
    assert not rows[str(plain.pk)]["open_listing"] and not rows[str(plain.pk)]["lost_report_id"]


@pytest.mark.django_db
def test_the_list_costs_the_same_queries_for_one_pet_or_many():
    def count(owner):
        with CaptureQueriesContext(connection) as ctx:
            assert c(owner).get("/api/v1/me/pets").status_code == 200
        return len(ctx.captured_queries)

    one, many = person(), person()
    p = _pet(one); _listing(one, p); _lost(one, p)
    for i in range(6):
        p = _pet(many, f"P{i}"); _listing(many, p); _lost(many, p)
    assert count(one) == count(many)
