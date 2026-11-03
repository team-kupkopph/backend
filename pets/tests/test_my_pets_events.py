"""Steps 13–14 of the My Pets review (docs/superpowers/specs/2026-10-28-my-pets-review.md) ·
PP19: every pet write records an account-free analytics event, on commit; PP11: GET /me/pets is
bounded."""
import json
import logging

import pytest

from listings.models import AdoptionListing
from listings.tests.adoption_helpers import c, inquire, listing, person, post
from pets.models import Pet


@pytest.fixture
def events(caplog):
    """The analytics events emitted so far, as dicts."""
    caplog.set_level(logging.INFO, logger="kupkop.analytics")

    def read():
        out = []
        for r in caplog.records:
            if r.name != "kupkop.analytics":
                continue
            msg = r.getMessage()
            out.append(json.loads(msg) if isinstance(msg, str) else msg)
        return out
    return read


def _names(events):
    return [e["event"] for e in events()]


def _add(owner, **extra):
    return c(owner).post("/api/v1/me/pets", {"name": "Mochi", "species": "cat", **extra}, format="json")


@pytest.mark.django_db
def test_adding_editing_and_removing_a_pet_each_record_one_event(events, django_capture_on_commit_callbacks):
    owner = person()
    with django_capture_on_commit_callbacks(execute=True):
        pid = _add(owner, photos=[{"file_url": f"https://bkt.example/listing_photo/{owner.pk}/a"}]).json()["pet_id"]
    with django_capture_on_commit_callbacks(execute=True):
        c(owner).patch(f"/api/v1/me/pets/{pid}", {"name": "Mochi II", "allergies": "Fish"}, format="json")
    with django_capture_on_commit_callbacks(execute=True):
        c(owner).delete(f"/api/v1/me/pets/{pid}", {"reason": "given_away"}, format="json")
    got = {e["event"]: e for e in events() if e["event"].startswith("pet_")}
    assert got["pet_added"] | {"ts": None} == {"event": "pet_added", "ts": None, "species": "cat", "has_photo": True,
                                               "source": "self"}
    assert got["pet_updated"]["fields"] == 2 and got["pet_updated"]["species"] == "cat"
    assert got["pet_removed"]["reason"] == "given_away"


@pytest.mark.django_db
def test_a_refused_write_records_nothing(events, django_capture_on_commit_callbacks):
    owner = person(member=True)
    pid = _add(owner).json()["pet_id"]
    c(owner).post("/api/v1/listings", {"pet": {"name": "Mochi", "species": "cat"}, "adoption_fee": "0",
                                       "city": "Marikina", "pet_id": pid}, format="json")
    before = len(events())
    with django_capture_on_commit_callbacks(execute=True):
        res = c(owner).delete(f"/api/v1/me/pets/{pid}", format="json")        # listed: 409
        bad = c(owner).patch(f"/api/v1/me/pets/{pid}", {"weight_kg": "0"}, format="json")
    assert res.status_code == 409 and bad.status_code == 400
    assert len(events()) == before


@pytest.mark.django_db
def test_rehoming_through_my_pets_is_measured_end_to_end(events, django_capture_on_commit_callbacks):
    a, b = person(member=True), person(member=True)
    pid = _add(a).json()["pet_id"]
    with django_capture_on_commit_callbacks(execute=True):
        lid = c(a).post("/api/v1/listings", {"pet": {"name": "Mochi", "species": "cat"}, "adoption_fee": "0",
                                             "city": "Marikina", "pet_id": pid}, format="json").json()["listing_id"]
    c(a).post(f"/api/v1/listings/{lid}/publish")
    inq = inquire(b, AdoptionListing.objects.get(pk=lid))
    post(a, inq, "screen"); post(a, inq, "reserve")
    with django_capture_on_commit_callbacks(execute=True):
        assert post(a, inq, "complete").status_code == 200
    got = [e for e in events() if e["event"] in ("pet_rehome_listed", "pet_transferred", "adoption_completed")]
    # Order isn't asserted: adoption_completed emits inline, the pet events on commit.
    assert sorted((e["event"], json.dumps({k: v for k, v in e.items() if k not in ("event", "ts")}, sort_keys=True))
                  for e in got) == sorted([
        ("pet_rehome_listed", json.dumps({"species": "cat"})),
        ("pet_transferred", json.dumps({"species": "cat", "via": "adoption"}, sort_keys=True)),
        ("adoption_completed", json.dumps({"rehome": True})),
    ])


@pytest.mark.django_db
def test_a_rescue_adoption_adds_a_pet_and_is_not_a_rehome(events, django_capture_on_commit_callbacks):
    poster, adopter = person(member=True), person(member=True)
    item = listing(poster)
    inq = inquire(adopter, item)
    post(poster, inq, "screen"); post(poster, inq, "reserve")
    with django_capture_on_commit_callbacks(execute=True):
        assert post(poster, inq, "complete").status_code == 200
    added = [e for e in events() if e["event"] == "pet_added"]
    assert [(e["source"], e["has_photo"]) for e in added] == [("adoption", False)]
    assert [e["rehome"] for e in events() if e["event"] == "adoption_completed"] == [False]


@pytest.mark.django_db
def test_mark_adopted_on_a_rehoming_listing_records_the_pet_leaving(events, django_capture_on_commit_callbacks):
    owner = person(member=True)
    pid = _add(owner).json()["pet_id"]
    lid = c(owner).post("/api/v1/listings", {"pet": {"name": "Mochi", "species": "cat"}, "adoption_fee": "0",
                                             "city": "Marikina", "pet_id": pid}, format="json").json()["listing_id"]
    c(owner).post(f"/api/v1/listings/{lid}/publish")
    with django_capture_on_commit_callbacks(execute=True):
        assert c(owner).post(f"/api/v1/listings/{lid}/mark-adopted", {}, format="json").status_code == 200
    assert {"event": "pet_removed", "species": "cat", "reason": "rehomed_outside"}.items() <= next(
        e for e in events() if e["event"] == "pet_removed").items()


@pytest.mark.django_db
def test_no_event_carries_an_account(events, django_capture_on_commit_callbacks):
    """The common/analytics.py contract: coarse dimensions only, never an id, email or name."""
    a, b = person(member=True), person(member=True)
    with django_capture_on_commit_callbacks(execute=True):
        pid = _add(a).json()["pet_id"]
        c(a).patch(f"/api/v1/me/pets/{pid}", {"temperament": "Shy"}, format="json")
    text = json.dumps(events())
    for secret in (str(a.pk), a.pk.hex, a.email, str(b.pk), pid, "Mochi"):
        assert secret not in text


@pytest.mark.django_db
def test_the_list_is_capped_at_a_hundred_newest_first():
    owner = person()
    Pet.objects.bulk_create([Pet(owner_account=owner, name=f"P{i:03d}", species="cat") for i in range(103)])
    body = c(owner).get("/api/v1/me/pets").json()
    assert len(body["results"]) == 100 and body["more"] is True
    small = person()
    _add(small)
    assert c(small).get("/api/v1/me/pets").json()["more"] is False
