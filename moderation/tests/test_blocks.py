"""Review step 7d (MO1, MQ1) · block a person. Both stores require it (Apple 1.2, Google's UGC policy).

MQ1, as the owner accepted it: a block works in both directions across stories, listings and contact; a blocked
person is never told, so every refusal is the same 404 a missing thing gives; and an adoption or rescue already under
way is never touched. Rescue reports are exempt on purpose: an animal in trouble outranks a block."""
import uuid

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.factories import AccountFactory
from community.models import StoryPost
from listings.models import ListingPreference, PreferenceKind
from listings.tests.adoption_helpers import c, inquire, listing, person
from moderation.blocks import hidden_from
from moderation.models import AccountBlock
from volunteer.models import VolunteerShift
from volunteer.tests.helpers import verified_shelter

URL = "/api/v1/me/blocks"


def _block(blocker, **body):
    return c(blocker).post(URL, body, format="json")


def _story(author):
    return StoryPost.objects.create(author_account=author, story_type="general", caption="a story")


def _shift(shelter):
    return VolunteerShift.objects.create(
        shelter_account=shelter, type="walking", capacity=2, status="open",
        starts_at=timezone.now() + timezone.timedelta(days=2), ends_at=timezone.now() + timezone.timedelta(days=2, hours=2))


# ---- the API ------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_block_a_person_by_id_and_see_them_listed():
    me, them = person(), person()
    res = _block(me, account_id=str(them.pk))
    assert res.status_code == 201, res.content
    assert set(res.json()) == {"block_id", "display_name"} and res.json()["display_name"] == them.display_name
    listed = c(me).get(URL).json()["results"]
    assert [r["display_name"] for r in listed] == [them.display_name] and listed[0]["block_id"] == res.json()["block_id"]
    assert str(them.pk) not in str(listed)                   # the account id is never handed back
    assert c(them).get(URL).json()["results"] == []          # and it is not mutual: they hold no block


@pytest.mark.django_db
@pytest.mark.parametrize("via", ["listing", "story", "shift"])
def test_block_the_person_behind_something_they_posted(via):
    owner = {"listing": person, "story": AccountFactory, "shift": verified_shelter}[via]()
    thing = {"listing": lambda: listing(owner), "story": lambda: _story(owner), "shift": lambda: _shift(owner)}[via]()
    me = person()
    res = _block(me, via_type=via, via_id=str(thing.pk))
    assert res.status_code == 201 and AccountBlock.objects.filter(blocker=me, blocked=owner).exists()
    assert str(owner.pk) not in str(res.json())


@pytest.mark.django_db
def test_blocking_twice_is_one_block():
    me, them = person(), person()
    assert _block(me, account_id=str(them.pk)).status_code == 201
    again = _block(me, account_id=str(them.pk))
    assert again.status_code == 200 and AccountBlock.objects.filter(blocker=me).count() == 1


@pytest.mark.django_db
def test_you_cannot_block_yourself_or_something_that_is_not_there_or_a_rescue_report():
    me = person()
    mine = listing(me)
    assert _block(me, account_id=str(me.pk)).status_code == 422
    assert _block(me, via_type="listing", via_id=str(mine.pk)).json()["error"]["code"] == "own_account"
    assert _block(me, account_id=str(uuid.uuid4())).status_code == 404
    assert _block(me, via_type="listing", via_id=str(uuid.uuid4())).status_code == 404
    assert _block(me, via_type="report", via_id=str(uuid.uuid4())).status_code == 400
    assert _block(me).status_code == 400 and _block(me, account_id=str(uuid.uuid4()), via_type="listing").status_code == 400
    assert not AccountBlock.objects.exists()


@pytest.mark.django_db
def test_a_guest_cannot_block():
    from rest_framework.test import APIClient
    assert APIClient().post(URL, {"account_id": str(person().pk)}, format="json").status_code == 401
    assert APIClient().get(URL).status_code == 401


@pytest.mark.django_db
def test_unblock_deletes_only_your_own_block():
    me, them, other = person(), person(), person()
    mine = _block(me, account_id=str(them.pk)).json()["block_id"]
    assert c(other).delete(f"{URL}/{mine}").status_code == 404           # not theirs to remove
    assert c(me).delete(f"{URL}/{uuid.uuid4()}").status_code == 404
    assert AccountBlock.objects.count() == 1
    assert c(me).delete(f"{URL}/{mine}").status_code == 204 and AccountBlock.objects.count() == 0


@pytest.mark.django_db
def test_the_database_itself_refuses_a_duplicate_or_a_self_block():
    a, b = person(), person()
    AccountBlock.objects.create(blocker=a, blocked=b)
    with pytest.raises(IntegrityError), transaction.atomic():
        AccountBlock.objects.create(blocker=a, blocked=b)
    with pytest.raises(IntegrityError), transaction.atomic():
        AccountBlock.objects.create(blocker=a, blocked=a)
    AccountBlock.objects.create(blocker=b, blocked=a)                   # the other direction is its own block


@pytest.mark.django_db
def test_hidden_from_is_both_directions_and_empty_for_a_guest():
    a, b, c_, d = person(), person(), person(), person()
    AccountBlock.objects.create(blocker=a, blocked=b)
    AccountBlock.objects.create(blocker=c_, blocked=a)
    assert hidden_from(a) == {b.pk, c_.pk} and hidden_from(b) == {a.pk} and hidden_from(d) == set()
    from django.contrib.auth.models import AnonymousUser
    assert hidden_from(AnonymousUser()) == set() and hidden_from(None) == set()


# ---- where it is enforced ------------------------------------------------------------------------
@pytest.mark.django_db
def test_stories_of_a_blocked_pair_leave_the_feed_the_page_and_reactions():
    a, b = person(), person()
    story = _story(b)
    feed = lambda who: [s["story_id"] for s in c(who).get("/api/v1/stories").json()["results"]]  # noqa: E731
    assert str(story.pk) in feed(a)
    for blocker, blocked in ((a, b), (b, a)):                       # either way round, a is the one who must not see it
        AccountBlock.objects.all().delete()
        AccountBlock.objects.create(blocker=blocker, blocked=blocked)
        assert str(story.pk) not in feed(a)
        hidden = c(a).get(f"/api/v1/stories/{story.pk}")
        absent = c(a).get(f"/api/v1/stories/{uuid.uuid4()}")
        assert hidden.status_code == absent.status_code == 404           # the same answer as a story that is not there
        assert hidden.json()["error"]["code"] == absent.json()["error"]["code"]
        assert c(a).post(f"/api/v1/stories/{story.pk}/reactions", {}, format="json").status_code == 404
    AccountBlock.objects.all().delete()
    assert str(story.pk) in feed(a) and c(a).get(f"/api/v1/stories/{story.pk}").status_code == 200


@pytest.mark.django_db
def test_a_block_never_hides_your_own_story_from_you():
    a, b = person(), person()
    AccountBlock.objects.create(blocker=a, blocked=b)
    theirs, mine = _story(b), _story(a)
    assert c(b).get(f"/api/v1/stories/{theirs.pk}").status_code == 200       # their own
    assert c(a).get(f"/api/v1/stories/{mine.pk}").status_code == 200         # my own
    assert c(b).get(f"/api/v1/stories/{mine.pk}").status_code == 404         # but hidden from the person I blocked


@pytest.mark.django_db
def test_listings_of_a_blocked_pair_leave_the_feed_the_page_the_saved_list_and_inquiries():
    a, b = person(), person()
    live = listing(b)
    ListingPreference.objects.create(account=a, listing=live, kind=PreferenceKind.SAVED)
    feed = lambda who: [r["listing_id"] for r in c(who).get("/api/v1/listings").json()["results"]]    # noqa: E731
    saved = lambda who: [r["listing_id"] for r in c(who).get("/api/v1/me/shortlist/listings").json()["results"]]  # noqa: E731
    assert str(live.pk) in feed(a) and str(live.pk) in saved(a)

    for blocker, blocked in ((a, b), (b, a)):
        AccountBlock.objects.all().delete()
        AccountBlock.objects.create(blocker=blocker, blocked=blocked)
        assert str(live.pk) not in feed(a) and str(live.pk) not in saved(a)
        assert c(a).get(f"/api/v1/listings/{live.pk}").status_code == 404
        refused = c(a).post(f"/api/v1/listings/{live.pk}/inquiries", {"message": ""}, format="json")
        assert refused.status_code == 404 and refused.json()["error"]["code"] == "not_found"
        assert c(b).get(f"/api/v1/listings/{live.pk}").status_code == 200   # the poster still has their own listing


@pytest.mark.django_db
def test_an_adoption_already_under_way_is_never_touched_by_a_block():
    """MQ1 · a block never reverses or hides something already in motion between the two."""
    adopter, poster = person(), person()
    live = listing(poster)
    inquiry = inquire(adopter, live)
    AccountBlock.objects.create(blocker=adopter, blocked=poster)
    assert any(r["inquiry_id"] == str(inquiry.pk) for r in c(adopter).get("/api/v1/me/inquiries").json()["results"])
    seen = c(poster).get(f"/api/v1/listings/{live.pk}/inquiries")
    assert seen.status_code == 200 and [r["inquiry_id"] for r in seen.json()["results"]] == [str(inquiry.pk)]
    assert c(poster).get(f"/api/v1/inquiries/{inquiry.pk}").status_code == 200
    assert c(adopter).get(f"/api/v1/inquiries/{inquiry.pk}").status_code == 200


@pytest.mark.django_db
def test_shifts_of_a_blocked_pair_leave_the_list_the_page_and_signups():
    volunteer, shelter = person(), verified_shelter()
    shift = _shift(shelter)
    listed = lambda who: [s["shift_id"] for s in c(who).get("/api/v1/shifts").json()["results"]]    # noqa: E731
    assert str(shift.pk) in listed(volunteer)
    for blocker, blocked in ((volunteer, shelter), (shelter, volunteer)):
        AccountBlock.objects.all().delete()
        AccountBlock.objects.create(blocker=blocker, blocked=blocked)
        assert str(shift.pk) not in listed(volunteer)
        assert c(volunteer).get(f"/api/v1/shifts/{shift.pk}").status_code == 404
        assert c(volunteer).post(f"/api/v1/shifts/{shift.pk}/signups", {"waiver_accepted": True}, format="json").status_code == 404
    AccountBlock.objects.all().delete()
    assert c(volunteer).get(f"/api/v1/shifts/{shift.pk}").status_code == 200


@pytest.mark.django_db
def test_a_block_between_two_other_people_changes_nothing_for_you():
    a, b, bystander = person(), person(), person()
    live, story = listing(b), _story(b)
    AccountBlock.objects.create(blocker=a, blocked=b)
    assert c(bystander).get(f"/api/v1/listings/{live.pk}").status_code == 200
    assert c(bystander).get(f"/api/v1/stories/{story.pk}").status_code == 200


@pytest.mark.django_db
def test_a_guest_sees_everything_a_block_is_between_accounts():
    a, b = person(), person()
    live = listing(b)
    AccountBlock.objects.create(blocker=a, blocked=b)
    from rest_framework.test import APIClient
    assert APIClient().get(f"/api/v1/listings/{live.pk}").status_code == 200


# ---- lifecycle -----------------------------------------------------------------------------------
@pytest.mark.django_db
def test_blocks_are_in_your_export_and_go_when_either_account_goes():
    from accounts.export import build_export
    me, them = person(), person()
    _block(me, account_id=str(them.pk))
    exported = build_export(me)["blocks"]
    assert [b["display_name"] for b in exported] == [them.display_name] and "account_id" not in exported[0]
    them.delete()
    assert not AccountBlock.objects.exists()
