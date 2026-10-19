"""AD7 · staff restore a listing a moderation takedown hid. It un-hides and does nothing else."""
import pytest

from adminapi.tests.conftest import full_signin
from listings.models import InquiryStatus, ListingStatus
from listings.moderation import hide_listing
from listings.tests.adoption_helpers import c, inquire, listing, person


@pytest.fixture
def auth(client, staffer):
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}


def _url(listing_id):
    return f"/admin-api/listings/{listing_id}/restore"


def _post(client, auth, listing_id, body):
    return client.post(_url(listing_id), body, content_type="application/json", **auth)


@pytest.mark.django_db
def test_restoring_a_hidden_listing_puts_it_back_on_the_feed_but_not_its_inquiries(client, auth):
    poster, adopter = person(), person()
    live = listing(poster)
    inq = inquire(adopter, live)
    hide_listing(live, person())
    assert c(adopter).get(f"/api/v1/listings/{live.pk}").status_code == 404

    res = _post(client, auth, live.pk, {"reason": "Flagged in error; it's a real rehoming."})

    assert res.status_code == 200, res.content
    assert res.json() == {"listing_id": str(live.pk), "status": "available", "hidden": False}
    assert c(adopter).get(f"/api/v1/listings/{live.pk}").status_code == 200
    inq.refresh_from_db()
    assert inq.status == InquiryStatus.WITHDRAWN        # a restore doesn't reopen what it closed
    assert live.status == ListingStatus.AVAILABLE


@pytest.mark.django_db
def test_restore_needs_a_reason_a_real_listing_and_a_hidden_one(client, auth):
    live = listing(person())
    assert _post(client, auth, live.pk, {"reason": "  "}).status_code == 422
    assert _post(client, auth, live.pk, {"reason": "x"}).status_code == 409   # not hidden
    assert _post(client, auth, "00000000-0000-0000-0000-000000000001",
                 {"reason": "x"}).status_code == 404
