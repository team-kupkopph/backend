"""My Pets review Phase 4, row I5 (NT-4) · GET /me/pets and GET /me/pets/{id} send exactly the keys
the app's types declare (`mobile_app/src/api/types.ts`: MyPet, RehomedPet, PetDetail,
PetListingState). A key added on one side and not the other fails here, in either direction.

`mobile_app` is a SEPARATE repository. In a backend-only checkout (what CI clones) there is nothing
to compare against, so the test skips; it runs wherever `mobile_app/` sits beside `backend/` (the
docs workspace), or point KUPKOP_MOBILE_APP at a checkout."""
import os
import pathlib
import re

import pytest
from django.contrib.gis.geos import Point

from listings.tests.adoption_helpers import c, inquire, person, post
from sagip.models import ReportType, StrayReport

ROOT = pathlib.Path(__file__).resolve().parents[3]  # the docs workspace, one level above backend/
TYPES = pathlib.Path(os.environ.get("KUPKOP_MOBILE_APP", ROOT / "mobile_app")) / "src" / "api" / "types.ts"

pytestmark = pytest.mark.skipif(
    not TYPES.exists(), reason="mobile_app not checked out beside backend/ (set KUPKOP_MOBILE_APP)")


def ts_keys(name):
    """The top-level keys of `export type <name> = { ... }` in types.ts, optional ones included."""
    source = TYPES.read_text()
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"//[^\n]*", "", source)
    match = re.search(rf"export type {name} = \{{", source)
    assert match, f"types.ts has no `export type {name} = {{`"
    depth, body = 1, []
    for ch in source[match.end():]:
        depth += (ch == "{") - (ch == "}")
        if depth == 0:
            break
        body.append(ch if depth == 1 else " ")     # blank out nested objects' own keys
    keys = set(re.findall(r"(\w+)\??\s*:", "".join(body)))
    assert keys, f"parsed no keys from {name}"       # guards under-report: never compare against nothing
    return keys


def _rehome(poster, adopter, pid):
    lid = c(poster).post("/api/v1/listings", {"pet": {"name": "Mochi", "species": "cat"}, "adoption_fee": "0",
                                              "city": "Marikina", "pet_id": pid}, format="json").json()["listing_id"]
    c(poster).post(f"/api/v1/listings/{lid}/publish")
    from listings.models import AdoptionListing
    inq = inquire(adopter, AdoptionListing.objects.get(pk=lid))
    post(poster, inq, "screen"); post(poster, inq, "reserve")
    assert post(poster, inq, "complete").status_code == 200


@pytest.fixture
def owner_with_everything(db):
    """An owner with a listed pet, a lost pet and a pet they rehomed: every optional block filled."""
    owner, adopter = person(member=True), person(member=True)

    def add(name):
        res = c(owner).post("/api/v1/me/pets", {"name": name, "species": "cat", "birthdate": "2022-03-01"},
                            format="json")
        assert res.status_code == 201, res.content
        return res.json()["pet_id"]

    listed, lost, gone = add("Mochi"), add("Ube"), add("Kape")
    assert c(owner).post("/api/v1/listings", {"pet": {"name": "Mochi", "species": "cat"}, "adoption_fee": "0",
                                              "city": "Marikina", "pet_id": listed},
                         format="json").status_code == 201
    StrayReport.objects.create(reporter_account=owner, report_type=ReportType.LOST, pet_id=lost,
                               species="cat", condition="healthy", geom=Point(121.1, 14.65))
    _rehome(owner, adopter, gone)
    return owner, listed


def test_the_my_pets_list_sends_what_the_app_declares(owner_with_everything):
    owner, listed = owner_with_everything
    body = c(owner).get("/api/v1/me/pets").json()
    rows = {row["pet_id"]: row for row in body["results"]}
    assert len(rows) == 2
    for row in rows.values():
        assert set(row) == ts_keys("MyPet")
    assert set(rows[listed]["open_listing"]) == ts_keys("PetListingState")
    [trace] = body["rehomed"]
    assert set(trace) == ts_keys("RehomedPet")


def test_the_pet_screen_sends_what_the_app_declares(owner_with_everything):
    owner, listed = owner_with_everything
    detail = c(owner).get(f"/api/v1/me/pets/{listed}").json()
    assert set(detail) == ts_keys("PetDetail")
    assert set(detail["open_listing"]) == ts_keys("PetListingState")
