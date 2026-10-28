"""AS5 / X5 · a suspended or deleted account's content is hidden from every public read (plan Task 5).

The review found three different spellings of "publicly visible": needs, shifts, the donation QR and Sagip
alerts already filtered on an ACTIVE account, but adoption listings and stories excluded only DELETED. So a
suspended poster (the person staff just acted against) kept their listing in the feed, its detail page
answered 200, and their story stayed in the story feed. One predicate now (`accounts/visibility.py`), and
reinstating shows everything again because nothing was ever deleted.

One test per surface, each asserting all three states: live, suspended (gone), reinstated (back).
"""
import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from accounts.models import Account
from accounts.visibility import account_is_public, public_account_q
from community.models import ShelterNeed, StoryPhoto, StoryPost
from listings.tests.adoption_helpers import c, listing, person, shelter


def _set(account, status):
    fields = {"status": status}
    if status == "deleted":
        fields["deleted_at"] = timezone.now()
    Account.objects.filter(pk=account.pk).update(**fields)


def _story(author):
    s = StoryPost.objects.create(author_account=author, story_type="general", caption="Adopted!", status="published")
    StoryPhoto.objects.create(story=s, url="https://x/0.jpg", is_primary=True)
    return s


def _ids(res):
    return {item.get("listing_id") or item.get("story_id") or item.get("need_id") for item in res.json()["results"]}


# ------------------------------------------------------------------------- the helper itself

@pytest.mark.django_db
def test_only_an_active_account_is_public():
    live, suspended, deleted = AccountFactory(), AccountFactory(), AccountFactory()
    _set(suspended, "suspended")
    _set(deleted, "deleted")
    for a in (live, suspended, deleted):
        a.refresh_from_db()

    assert [account_is_public(a) for a in (live, suspended, deleted)] == [True, False, False]
    public = set(Account.objects.filter(public_account_q()).values_list("pk", flat=True))
    assert live.pk in public and suspended.pk not in public and deleted.pk not in public


@pytest.mark.django_db
def test_the_prefix_follows_a_relation():
    from community.models import ShelterNeed as Need
    live, gone = AccountFactory(account_type="shelter"), AccountFactory(account_type="shelter")
    _set(gone, "suspended")
    n1 = Need.objects.create(shelter_account=live, title="a", category="food", quantity_needed=1, quantity_received=0)
    n2 = Need.objects.create(shelter_account=gone, title="b", category="food", quantity_needed=1, quantity_received=0)

    shown = set(Need.objects.filter(public_account_q("shelter_account__")).values_list("pk", flat=True))

    assert n1.pk in shown and n2.pk not in shown


# ------------------------------------------------------------------------- adoption listings

@pytest.mark.django_db
@pytest.mark.parametrize("status", ["suspended", "deleted"])
def test_a_suspended_or_deleted_posters_listing_leaves_browse_and_detail(status):
    poster, viewer = person(member=True), person()
    mine = listing(poster)

    live = c(viewer).get("/api/v1/listings?city=Marikina")
    _set(poster, status)
    gone_feed = c(viewer).get("/api/v1/listings?city=Marikina")
    gone_detail = c(viewer).get(f"/api/v1/listings/{mine.pk}")

    assert str(mine.pk) in _ids(live)
    assert str(mine.pk) not in _ids(gone_feed)
    assert gone_detail.status_code == 404


@pytest.mark.django_db
def test_reinstating_brings_the_listing_back():
    poster, viewer = person(member=True), person()
    mine = listing(poster)
    _set(poster, "suspended")

    _set(poster, "active")

    assert str(mine.pk) in _ids(c(viewer).get("/api/v1/listings?city=Marikina"))
    assert c(viewer).get(f"/api/v1/listings/{mine.pk}").status_code == 200


@pytest.mark.django_db
def test_nobody_can_start_an_inquiry_on_a_suspended_posters_listing():
    poster, adopter = person(member=True), person(member=True)
    mine = listing(poster)
    _set(poster, "suspended")

    res = c(adopter).post(f"/api/v1/listings/{mine.pk}/inquiries", {"message": "hi"}, format="json")

    assert res.status_code == 404


@pytest.mark.django_db
def test_a_suspended_posters_listing_leaves_someones_saved_list():
    poster, viewer = person(member=True), person()
    mine = listing(poster)
    c(viewer).put(f"/api/v1/me/shortlist/{mine.pk}", {"kind": "saved"}, format="json")   # saved while the poster was live
    saved_before = c(viewer).get("/api/v1/me/shortlist/listings")
    _set(poster, "suspended")

    saved_after = c(viewer).get("/api/v1/me/shortlist/listings")

    assert saved_before.status_code == 200 and saved_after.status_code == 200
    assert str(mine.pk) in _ids(saved_before)
    assert str(mine.pk) not in _ids(saved_after)


@pytest.mark.django_db
def test_the_poster_still_sees_their_own_listing_when_suspended():
    """Hidden from OTHER people. A suspended poster is signed out anyway (sessions are revoked), but the row
    and its owner's record survive: nothing is deleted."""
    poster = person(member=True)
    mine = listing(poster)

    _set(poster, "suspended")

    assert type(mine).objects.filter(pk=mine.pk).exists()


# ------------------------------------------------------------------------- stories

@pytest.mark.django_db
@pytest.mark.parametrize("status", ["suspended", "deleted"])
def test_a_suspended_or_deleted_authors_story_leaves_the_feed_and_its_detail(status):
    author = AccountFactory()
    s = _story(author)
    anon = APIClient()

    before = anon.get("/api/v1/stories")
    _set(author, status)
    feed = anon.get("/api/v1/stories")
    detail = anon.get(f"/api/v1/stories/{s.pk}")

    assert str(s.pk) in _ids(before)
    assert str(s.pk) not in _ids(feed)
    assert detail.status_code == 404


@pytest.mark.django_db
def test_reinstating_brings_the_story_back():
    author = AccountFactory()
    s = _story(author)
    _set(author, "suspended")

    _set(author, "active")

    assert str(s.pk) in _ids(APIClient().get("/api/v1/stories"))
    assert APIClient().get(f"/api/v1/stories/{s.pk}").status_code == 200


@pytest.mark.django_db
def test_nobody_can_react_to_a_suspended_authors_story():
    author, fan = AccountFactory(), AccountFactory()
    s = _story(author)
    _set(author, "suspended")

    res = c(fan).post(f"/api/v1/stories/{s.pk}/reactions")

    assert res.status_code == 404


# ------------------------------------------------------------------------- needs (already hid; pinned)

@pytest.mark.django_db
def test_a_suspended_shelters_needs_leave_the_public_wishlist_and_return():
    shl = shelter()
    ShelterNeed.objects.create(shelter_account=shl, title="Dog food", category="food",
                               quantity_needed=5, quantity_received=0, status="open")
    anon = APIClient()

    live = anon.get(f"/api/v1/shelters/{shl.pk}/needs")
    _set(shl, "suspended")
    gone = anon.get(f"/api/v1/shelters/{shl.pk}/needs")
    _set(shl, "active")
    back = anon.get(f"/api/v1/shelters/{shl.pk}/needs")

    assert len(live.json()["results"]) == 1
    assert not gone.json().get("results")
    assert len(back.json()["results"]) == 1
