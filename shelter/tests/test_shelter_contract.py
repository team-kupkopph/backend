"""NT-6 (shelter home test plan: U10, I8) · the app's shelter types match what the server sends, both ways.

`mobile_app/src/api/types.ts` declares `ShelterRequest`, `ShelterRequestKind`, `ShelterRequestStatus`, `ShelterDashboard`
and its blocks. A key or status added on one side and not the other fails here. The status union is checked against the
backend's own enums, so a new `InquiryStatus` or `SignupStatus` value cannot reach the app without a chip
(`REQUEST_STATUS_TONE` is keyed by the union, so tsc then fails the app until it has one).

`mobile_app` is a SEPARATE repository: in a backend-only checkout (what CI clones) this skips, like the My Pets contract
test whose parser it reuses. It runs wherever `mobile_app/` sits beside `backend/`, or with KUPKOP_MOBILE_APP set.
"""
import re
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from listings.models import AdoptionInquiry, AdoptionListing, InquiryStatus
from pets.tests.test_my_pets_contract import TYPES, ts_keys
from shelter.models import ShelterProfile
from shelter.views import REQUEST_KINDS
from volunteer.models import ShiftStatus, SignupStatus, VolunteerShift, VolunteerSignup

pytestmark = pytest.mark.skipif(
    not TYPES.exists(), reason="mobile_app not checked out beside backend/ (set KUPKOP_MOBILE_APP)")


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def ts_union(name):
    """The string literals of `export type <name> = "a" | "b" ...`."""
    source = re.sub(r"//[^\n]*", "", TYPES.read_text())
    match = re.search(rf"export type {name} =([^;]*);", source, flags=re.S)
    assert match, f"types.ts has no `export type {name} = ...;`"
    values = set(re.findall(r'"(\w+)"', match.group(1)))
    assert values, f"parsed no members from {name}"
    return values


def ts_inline_keys(type_name, field):
    """The keys of an inline object field, e.g. `counts: { ... }` inside `ShelterDashboard`."""
    source = re.sub(r"//[^\n]*", "", TYPES.read_text())
    block = re.search(rf"export type {type_name} = \{{(.*?)\n\}};", source, flags=re.S)
    assert block, type_name
    match = re.search(rf"\b{field}\??\s*:\s*\{{", block.group(1))
    assert match, f"{type_name}.{field} is not an inline object"
    depth, body = 1, []
    for ch in block.group(1)[match.end():]:
        depth += (ch == "{") - (ch == "}")
        if depth == 0:
            break
        body.append(ch if depth == 1 else " ")     # this object's own keys, not a nested one's
    keys = set(re.findall(r"(\w+)\??\s*:", "".join(body)))
    assert keys, f"parsed no keys from {type_name}.{field}"
    return keys


@pytest.fixture
def shelter_with_one_of_each(db):
    shelter = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    ShelterProfile.objects.create(account=shelter, org_name="Paws", org_type="shelter")
    listing = AdoptionListing.objects.create(posted_by=shelter, name="Bantay", species="dog", city="Marikina",
                                             adoption_fee="0")
    AdoptionInquiry.objects.create(listing=listing, adopter_account=AccountFactory(account_type="personal"))
    worker = AccountFactory(account_type="personal")
    placed = AdoptionListing.objects.create(posted_by=worker, name="Kalabaw", species="dog", city="Marikina",
                                            adoption_fee="0", status="pending")
    AdoptionInquiry.objects.create(listing=placed, adopter_account=shelter, kind="placement")
    starts = timezone.now() + timedelta(days=2)
    shift = VolunteerShift.objects.create(shelter_account=shelter, type="walking", starts_at=starts,
                                          ends_at=starts + timedelta(hours=2), capacity=3, status=ShiftStatus.OPEN)
    VolunteerSignup.objects.create(shift=shift, volunteer_account=AccountFactory(account_type="personal"))
    return shelter


def test_every_inbox_key_is_typed_and_every_typed_key_is_sent(client, shelter_with_one_of_each):
    rows = client.get("/api/v1/shelter/requests", **_hdr(shelter_with_one_of_each)).json()["results"]
    assert {r["kind"] for r in rows} == {"adoption", "volunteer", "placement"}     # one of each, or this proves little
    sent = set().union(*(set(r) for r in rows))
    typed = ts_keys("ShelterRequest")
    assert sent - typed == set(), f"sent but not typed: {sent - typed}"
    assert typed - sent == set(), f"typed but never sent: {typed - sent}"


def test_the_kind_union_is_the_servers_kinds():
    assert ts_union("ShelterRequestKind") == set(REQUEST_KINDS)


def test_the_status_union_is_exactly_the_backends_two_vocabularies():
    assert ts_union("ShelterRequestStatus") == set(InquiryStatus.values) | set(SignupStatus.values)


def test_the_dashboard_and_its_blocks_match_the_types(client, shelter_with_one_of_each):
    body = client.get("/api/v1/shelter/dashboard", **_hdr(shelter_with_one_of_each)).json()
    assert set(body) == ts_keys("ShelterDashboard")
    for field in ("verification", "counts", "gates"):
        assert set(body[field]) == ts_inline_keys("ShelterDashboard", field), field
    assert set(body["volunteer"]) == ts_keys("ShelterDashboardVolunteer")
    assert set(body["rescue"]) == ts_keys("ShelterDashboardRescue")
    assert set(body["wishlist"]) == ts_keys("ShelterDashboardWishlist")
