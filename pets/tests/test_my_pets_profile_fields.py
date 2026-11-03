"""Step 12 of the My Pets review (docs/superpowers/specs/2026-10-28-my-pets-review.md) · PQ4: every
Feature Spec §3.2 field the `pet` table has can be entered, edited, read back, exported, and travels
with the pet when it is rehomed."""
import pytest

from listings.models import AdoptionListing
from listings.tests.adoption_helpers import c, inquire, person, post
from pets.models import Pet

FULL = {"name": "Mochi", "species": "cat", "birthdate": "2023-03-01", "dob_approximate": True,
        "weight_kg": "4.20", "microchip_no": "985-112009", "feeding_routine": "Twice a day, wet food at night",
        "allergies": "Chicken", "medications": "None", "medical_conditions": "Mild asthma",
        "vet_contact": "Dr Reyes", "emergency_contact": "Ana, 0918 000 1111"}
NEW = ("dob_approximate", "weight_kg", "microchip_no", "feeding_routine", "allergies", "medications",
       "emergency_contact")


def _add(owner, **over):
    res = c(owner).post("/api/v1/me/pets", {**FULL, **over}, format="json")
    assert res.status_code == 201, res.content
    return res.json()["pet_id"]


def _detail(owner, pid):
    return c(owner).get(f"/api/v1/me/pets/{pid}").json()


@pytest.mark.django_db
def test_every_new_field_is_kept_and_read_back():
    owner = person()
    d = _detail(owner, _add(owner))
    for key in NEW:
        assert d[key] == FULL[key], key


@pytest.mark.django_db
def test_each_field_can_be_edited_and_cleared():
    owner = person()
    pid = _add(owner)
    res = c(owner).patch(f"/api/v1/me/pets/{pid}", {"weight_kg": "5.5", "allergies": "", "medications": "Inhaler",
                                                    "microchip_no": "", "emergency_contact": "Ben"}, format="json")
    assert res.status_code == 200, res.content
    d = _detail(owner, pid)
    assert (d["weight_kg"], d["allergies"], d["medications"], d["microchip_no"], d["emergency_contact"]) == (
        "5.50", None, "Inhaler", None, "Ben")
    c(owner).patch(f"/api/v1/me/pets/{pid}", {"weight_kg": None}, format="json")
    assert _detail(owner, pid)["weight_kg"] is None


@pytest.mark.django_db
def test_approximate_needs_a_birthdate():
    owner = person()
    assert _detail(owner, _add(owner, birthdate=None))["dob_approximate"] is False   # nothing to be approximate about
    pid = _add(owner)
    c(owner).patch(f"/api/v1/me/pets/{pid}", {"birthdate": None}, format="json")
    assert Pet.objects.get(pk=pid).dob_approximate is False                          # cleared with the date
    c(owner).patch(f"/api/v1/me/pets/{pid}", {"birthdate": "2022-06-01", "dob_approximate": True}, format="json")
    assert _detail(owner, pid)["dob_approximate"] is True


@pytest.mark.django_db
@pytest.mark.parametrize("field,value", [
    ("weight_kg", "0"), ("weight_kg", "-1"), ("weight_kg", "1000"), ("weight_kg", "4.123"), ("weight_kg", "heavy"),
    ("microchip_no", "985 112"), ("microchip_no", "x" * 41), ("microchip_no", "98#5"),
    ("feeding_routine", "x" * 1001), ("allergies", "x" * 1001), ("medications", "x" * 1001),
    ("emergency_contact", "x" * 161),
])
def test_a_bad_value_is_refused_on_its_field(field, value):
    owner = person()
    res = c(owner).post("/api/v1/me/pets", {**FULL, field: value}, format="json")
    assert res.status_code == 400 and field in res.json()["error"]["details"], res.content
    assert not Pet.objects.exists()


@pytest.mark.django_db
def test_the_edges_are_accepted():
    owner = person()
    pid = _add(owner, weight_kg="0.01", microchip_no="A" * 40, feeding_routine="x" * 1000)
    assert _detail(owner, pid)["weight_kg"] == "0.01"
    pid = _add(owner, weight_kg="999.99")
    assert _detail(owner, pid)["weight_kg"] == "999.99"


@pytest.mark.django_db
def test_every_field_travels_with_the_pet_when_it_is_rehomed():
    a, b = person(member=True), person(member=True)
    pid = _add(a)
    lid = c(a).post("/api/v1/listings", {"pet": {"name": "Mochi", "species": "cat"}, "adoption_fee": "0",
                                         "city": "Marikina", "pet_id": pid}, format="json").json()["listing_id"]
    c(a).post(f"/api/v1/listings/{lid}/publish")
    inq = inquire(b, AdoptionListing.objects.get(pk=lid))
    post(a, inq, "screen"); post(a, inq, "reserve")
    assert post(a, inq, "complete").status_code == 200
    d = _detail(b, pid)
    for key in (*NEW, "medical_conditions", "vet_contact", "birthdate"):
        assert d[key] == FULL[key], key
    [row] = c(b).get("/api/v1/me/export").json()["pets"]
    assert all(row[k] == FULL[k] for k in NEW)


@pytest.mark.django_db
def test_a_listing_made_from_the_pet_keeps_the_birthdate_approximate():
    owner = person(member=True)
    pid = _add(owner)
    body = {"pet": {"name": "Mochi", "species": "cat"}, "adoption_fee": "0", "city": "Marikina", "pet_id": pid}
    item = AdoptionListing.objects.get(pk=c(owner).post("/api/v1/listings", body, format="json").json()["listing_id"])
    assert (str(item.date_of_birth), item.dob_approximate) == ("2023-03-01", True)
    item.delete()
    typed = {**body, "pet": {**body["pet"], "birthdate": "2023-04-15"}}     # the owner's own date: not approximate
    item = AdoptionListing.objects.get(pk=c(owner).post("/api/v1/listings", typed, format="json").json()["listing_id"])
    assert (str(item.date_of_birth), item.dob_approximate) == ("2023-04-15", False)
