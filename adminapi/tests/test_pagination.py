"""US-C1 · the cursor is opaque to the client, and a broken one must not be a 500.

`decode_cursor` already treats undecodable input as "start from the top" and says why: a
malformed cursor is a client bug, and an error strands a reviewer on a broken bookmark.
These tests hold `paginate` to the same contract for the cursors that DO decode — to the
wrong shape, or to values the ordering fields cannot take.
"""
import pytest

from adminapi.pagination import decode_cursor, encode_cursor, paginate
from adminapi.tests.conftest import full_signin
from adminapi.tests.test_verifications import make_request
from verifications.models import VerificationRequest

KEYS = ["submitted_at", "verification_id"]


def _ordered():
    return VerificationRequest.objects.order_by(*KEYS)


@pytest.mark.django_db
@pytest.mark.parametrize("cursor_value", [
    [],                                   # decoded, but empty
    ["2026-01-01T00:00:00+00:00"],        # too short for a two-field ordering
    "2026-01-01T00:00:00+00:00",          # a JSON string, not a list
    42,                                   # a JSON number
    {"submitted_at": "x"},                # a JSON object
    None,                                 # what decode_cursor returns for garbage
])
def test_a_cursor_of_the_wrong_shape_starts_from_the_top(cursor_value):
    for i in range(3):
        make_request(name=f"Org {i}")
    rows, _ = paginate(_ordered(), cursor_value, KEYS)
    assert len(rows) == 3


@pytest.mark.django_db
@pytest.mark.parametrize("cursor_value", [
    ["not-a-datetime", "not-a-uuid"],
    ["2026-01-01T00:00:00+00:00", "not-a-uuid"],
    ["not-a-datetime", "00000000-0000-0000-0000-000000000000"],
])
def test_a_cursor_with_values_the_fields_reject_starts_from_the_top(cursor_value):
    """Right shape, wrong content — Django refuses these at `.filter()` time."""
    for i in range(3):
        make_request(name=f"Org {i}")
    rows, _ = paginate(_ordered(), cursor_value, KEYS)
    assert len(rows) == 3


@pytest.mark.django_db
def test_a_well_formed_cursor_still_pages():
    """The guard must not swallow the real thing."""
    for i in range(3):
        make_request(name=f"Org {i}")
    first, cursor = paginate(_ordered(), None, KEYS, page_size=2)
    assert len(first) == 2 and cursor
    rest, more = paginate(_ordered(), decode_cursor(cursor), KEYS, page_size=2)
    assert len(rest) == 1 and more is None
    assert {r.pk for r in first} | {r.pk for r in rest} == set(_ordered().values_list("pk", flat=True))


@pytest.mark.django_db
@pytest.mark.parametrize("cursor", [
    "not-base64!",                          # undecodable
    encode_cursor(["2026-01-01T00:00:00"]),  # decodes, too short
    encode_cursor(["garbage", "garbage"]),   # decodes, values rejected
])
def test_over_http_a_broken_cursor_is_a_200_first_page(client, staffer, cursor):
    """Asserted at the route, because that is where the 500 would have surfaced."""
    make_request()
    auth = {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}
    res = client.get(f"/admin-api/verifications?cursor={cursor}", **auth)
    assert res.status_code == 200
    assert len(res.json()["results"]) == 1
