"""The Adopt deck's shortlist — GET /me/shortlist, PUT/DELETE /me/shortlist/{listing_id}.

A preference is about the person: exported with their data, deleted on anonymization, invisible
to any other account. The writes are idempotent so an optimistic client can retry them."""
import uuid

import pytest
from django.utils import timezone

from accounts.export import build_export
from accounts.factories import AccountFactory
from accounts.purge import anonymize_account
from accounts.tokens import tokens_for
from listings.models import AdoptionListing, ListingPreference


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _listing(poster, **kw):
    defaults = dict(name="Bantay", species="dog", city="Marikina", adoption_fee="300.00")
    defaults.update(kw)
    return AdoptionListing.objects.create(posted_by=poster, **defaults)


def _put(client, acc, listing, kind):
    return client.put(f"/api/v1/me/shortlist/{listing.pk}", {"kind": kind},
                      content_type="application/json", **_hdr(acc))


@pytest.mark.django_db
def test_save_then_read_back_newest_first(client):
    me, poster = AccountFactory(), AccountFactory()
    a, b = _listing(poster, name="A"), _listing(poster, name="B")
    assert _put(client, me, a, "saved").status_code == 200
    assert _put(client, me, b, "hidden").status_code == 200
    body = client.get("/api/v1/me/shortlist", **_hdr(me)).json()
    assert body == {"saved": [str(a.pk)], "hidden": [str(b.pk)]}


@pytest.mark.django_db
def test_a_save_after_a_hide_replaces_the_row(client):
    """The deck's rule — saved XOR hidden — holds on the server: one row per pair."""
    me, poster = AccountFactory(), AccountFactory()
    listing = _listing(poster)
    _put(client, me, listing, "hidden")
    res = _put(client, me, listing, "saved")
    assert res.status_code == 200 and res.json()["kind"] == "saved"
    assert ListingPreference.objects.filter(account=me).count() == 1
    body = client.get("/api/v1/me/shortlist", **_hdr(me)).json()
    assert body == {"saved": [str(listing.pk)], "hidden": []}


@pytest.mark.django_db
def test_put_and_delete_are_idempotent(client):
    me, poster = AccountFactory(), AccountFactory()
    listing = _listing(poster)
    assert _put(client, me, listing, "saved").status_code == 200
    assert _put(client, me, listing, "saved").status_code == 200
    assert ListingPreference.objects.filter(account=me).count() == 1
    assert client.delete(f"/api/v1/me/shortlist/{listing.pk}", **_hdr(me)).status_code == 204
    assert client.delete(f"/api/v1/me/shortlist/{listing.pk}", **_hdr(me)).status_code == 204
    assert client.get("/api/v1/me/shortlist", **_hdr(me)).json() == {"saved": [], "hidden": []}


@pytest.mark.django_db
def test_rejects_an_unknown_kind_and_an_unknown_listing(client):
    me, poster = AccountFactory(), AccountFactory()
    listing = _listing(poster)
    assert _put(client, me, listing, "loved").status_code == 422
    res = client.put(f"/api/v1/me/shortlist/{uuid.uuid4()}", {"kind": "saved"},
                     content_type="application/json", **_hdr(me))
    assert res.status_code == 404


@pytest.mark.django_db
def test_another_account_sees_nothing_of_mine(client):
    me, other, poster = AccountFactory(), AccountFactory(), AccountFactory()
    _put(client, me, _listing(poster), "saved")
    assert client.get("/api/v1/me/shortlist", **_hdr(other)).json() == {"saved": [], "hidden": []}


@pytest.mark.django_db
def test_requires_auth(client):
    assert client.get("/api/v1/me/shortlist").status_code == 401


@pytest.mark.django_db
def test_a_deleted_listing_takes_its_preferences_with_it(client):
    me, poster = AccountFactory(), AccountFactory()
    listing = _listing(poster)
    _put(client, me, listing, "saved")
    listing.delete()
    assert client.get("/api/v1/me/shortlist", **_hdr(me)).json() == {"saved": [], "hidden": []}


@pytest.mark.django_db
def test_exported_with_the_account_and_gone_when_anonymized(client):
    me, poster = AccountFactory(), AccountFactory()
    listing = _listing(poster, name="Milo")
    _put(client, me, listing, "saved")
    export = build_export(me)
    assert export["shortlist"] == [
        {"listing_id": str(listing.pk), "listing_name": "Milo", "kind": "saved",
         "created_at": export["shortlist"][0]["created_at"]}
    ]
    anonymize_account(me, now=timezone.now())
    assert ListingPreference.objects.filter(account=me).count() == 0
