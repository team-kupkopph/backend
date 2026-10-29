"""Step 6 of the My Pets review (docs/superpowers/specs/2026-10-28-my-pets-review.md) · one set of
rules for an animal's fields (PP7), the database holding them too (PP15), stray_report.pet_id as a
real foreign key and one way to say "unknown sex" in a response (PP16)."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.gis.geos import Point
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.factories import AccountFactory
from listings.models import AdoptionListing, ListingStatus
from listings.tests.adoption_helpers import c, listing, person
from pets.models import Pet, PetPhoto
from sagip.models import StrayReport

TODAY = timezone.localdate()
TOMORROW = (TODAY + timedelta(days=1)).isoformat()


def _pet(owner, **kw):
    return Pet.objects.create(owner_account=owner, **{"name": "Mochi", "species": "cat", **kw})


def _refused(fn):
    with pytest.raises(IntegrityError), transaction.atomic():
        fn()


# ── PP7 · one validator ─────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_no_writer_takes_a_birthdate_in_the_future():
    owner = person(member=True)
    pet = _pet(owner)
    item = listing(owner)
    writes = [
        c(owner).post("/api/v1/me/pets", {"name": "A", "species": "cat", "birthdate": TOMORROW}, format="json"),
        c(owner).patch(f"/api/v1/me/pets/{pet.pk}", {"birthdate": TOMORROW}, format="json"),
        c(owner).post("/api/v1/listings", {"pet": {"name": "A", "species": "cat", "birthdate": TOMORROW},
                                           "adoption_fee": "0", "city": "Marikina"}, format="json"),
        c(owner).patch(f"/api/v1/listings/{item.pk}", {"birthdate": TOMORROW}, format="json"),
    ]
    for res in writes:
        assert res.status_code == 400, res.content
        assert "birthdate" in str(res.json()), res.content
    pet.refresh_from_db(); item.refresh_from_db()
    assert pet.date_of_birth is None and item.date_of_birth is None


@pytest.mark.django_db
def test_today_is_a_fine_birthday():
    owner = person()
    res = c(owner).post("/api/v1/me/pets", {"name": "Bagong", "species": "dog", "birthdate": TODAY.isoformat()},
                        format="json")
    assert res.status_code == 201, res.content


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["", "   ", "\t"])
def test_a_name_of_only_spaces_is_no_name(name):
    owner = person()
    res = c(owner).post("/api/v1/me/pets", {"name": name, "species": "cat"}, format="json")
    assert res.status_code == 400 and not Pet.objects.exists()


# ── PP15 · the database holds the rules too ─────────────────────────────────────────────────

@pytest.mark.django_db
def test_the_database_refuses_a_blank_name_and_a_weight_of_zero_or_less():
    owner = AccountFactory()
    _refused(lambda: _pet(owner, name="   "))
    _refused(lambda: _pet(owner, weight_kg=0))
    _refused(lambda: _pet(owner, weight_kg=-1))
    assert _pet(owner, weight_kg=Decimal("4.20")).weight_kg > 0
    assert _pet(owner, weight_kg=None).pk


@pytest.mark.django_db
def test_the_database_allows_one_primary_photo_per_pet():
    pet = _pet(AccountFactory())
    PetPhoto.objects.create(pet=pet, url="a", is_primary=True)
    _refused(lambda: PetPhoto.objects.create(pet=pet, url="b", is_primary=True))
    PetPhoto.objects.create(pet=pet, url="c", is_primary=False)
    PetPhoto.objects.create(pet=_pet(pet.owner_account), url="d", is_primary=True)   # another pet: fine


@pytest.mark.django_db
def test_the_database_allows_one_open_rehoming_listing_per_pet():
    owner = AccountFactory()
    pet = _pet(owner)

    def make(status):
        return AdoptionListing.objects.create(posted_by=owner, rehome_pet=pet, name="Mochi", species="cat",
                                              city="Marikina", status=status)

    first = make(ListingStatus.DRAFT)
    for status in (ListingStatus.DRAFT, ListingStatus.AVAILABLE, ListingStatus.PENDING):
        _refused(lambda status=status: make(status))
    make(ListingStatus.WITHDRAWN); make(ListingStatus.ADOPTED)            # closed ones never block
    AdoptionListing.objects.filter(pk=first.pk).update(status=ListingStatus.ADOPTED)
    assert make(ListingStatus.AVAILABLE).pk                               # PP1: rehomed again


# ── PP16 · a lost report's pet is a real link ───────────────────────────────────────────────

@pytest.mark.django_db
def test_a_lost_report_outlives_its_pet_and_loses_only_the_link():
    owner = AccountFactory()
    pet = _pet(owner)
    report = StrayReport.objects.create(reporter_account=owner, report_type="lost", species="cat",
                                        condition="healthy", geom=Point(121.1, 14.65), pet=pet)
    assert report.pet_id == pet.pk and list(pet.lost_reports.all()) == [report]
    pet.delete()                                                           # what an owner's purge does
    report.refresh_from_db()
    assert report.pet_id is None


@pytest.mark.django_db
def test_a_report_cannot_name_a_pet_that_does_not_exist():
    import uuid

    from django.db import connection
    owner = AccountFactory()
    with pytest.raises(IntegrityError), transaction.atomic():
        # Django's foreign keys are DEFERRABLE INITIALLY DEFERRED: checked at commit. Ask now.
        with connection.cursor() as cur:
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        StrayReport.objects.create(reporter_account=owner, report_type="lost", species="cat",
                                   condition="healthy", geom=Point(121.1, 14.65), pet_id=uuid.uuid4())


# ── PP16 · "unknown" reads the same everywhere ──────────────────────────────────────────────

@pytest.mark.django_db
def test_unknown_sex_reads_as_null_on_a_pet_and_on_a_listing():
    owner = person(member=True)
    pet = _pet(owner, sex="unknown")
    assert c(owner).get(f"/api/v1/me/pets/{pet.pk}").json()["sex"] is None
    for sex in ("", "unknown"):
        item = listing(owner, sex=sex)
        assert c(owner).get(f"/api/v1/listings/{item.pk}").json()["pet"]["sex"] is None
    assert c(owner).get(f"/api/v1/me/pets/{_pet(owner, sex='male').pk}").json()["sex"] == "male"
