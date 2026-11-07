"""Review step 4 (MO3) · the way back from a mistaken takedown, in the console: stories get a restore like listings and
reports have, the flag detail says what restoring would do, and (with step 5) a stale sibling flag cannot undo it."""
import pytest

from accounts.factories import AccountFactory
from adminapi.models import AdminAuditLog
from adminapi.tests.conftest import full_signin
from community.models import StoryPost, StoryStatus
from listings.tests.adoption_helpers import listing, person
from moderation.models import ModerationFlag


@pytest.fixture
def auth(client, staffer):
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}


def _post(client, auth, path, body=None):
    return client.post(path, body or {}, content_type="application/json", **auth)


def _story(status=StoryStatus.HIDDEN):
    return StoryPost.objects.create(author_account=AccountFactory(), story_type="general", caption="x", status=status)


def _flag(target_type, pk, status="open"):
    return ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type=target_type, target_id=pk,
                                         reason="x", status=status)


# ---- the story restore route ---------------------------------------------------------------------
@pytest.mark.django_db
def test_restoring_a_hidden_story_puts_it_back_in_the_feed(client, auth):
    story = _story()
    assert client.get("/api/v1/stories").json()["results"] == []

    res = _post(client, auth, f"/admin-api/stories/{story.pk}/restore", {"reason": "Flagged in error; it is a real adoption."})

    assert res.status_code == 200, res.content
    assert res.json() == {"story_id": str(story.pk), "status": "published", "hidden": False}
    assert [s["story_id"] for s in client.get("/api/v1/stories").json()["results"]] == [str(story.pk)]
    assert AdminAuditLog.objects.filter(action="stories/{id}/restore").first().detail == {"notes_len": 40, "restored": "story"}


@pytest.mark.django_db
def test_story_restore_checks_in_the_documented_order_404_422_409(client, auth):
    """Same order as the listing and report restores: an id that names nothing is 404 whatever the body says."""
    import uuid
    live, hidden = _story(StoryStatus.PUBLISHED), _story()
    assert _post(client, auth, f"/admin-api/stories/{uuid.uuid4()}/restore", {}).status_code == 404
    assert _post(client, auth, f"/admin-api/stories/{hidden.pk}/restore", {"reason": "  "}).status_code == 422
    assert _post(client, auth, f"/admin-api/stories/{live.pk}/restore", {"reason": "x"}).status_code == 409
    assert _post(client, auth, f"/admin-api/stories/{live.pk}/restore", {"reason": "x"}).json()["error"]["code"] == "not_hidden"
    hidden.refresh_from_db()
    assert hidden.status == StoryStatus.HIDDEN


@pytest.mark.django_db
def test_story_restore_needs_a_staff_token(client):
    story = _story()
    assert client.post(f"/admin-api/stories/{story.pk}/restore", {"reason": "x"}, content_type="application/json").status_code in (401, 403)
    story.refresh_from_db()
    assert story.status == StoryStatus.HIDDEN


# ---- the flag detail says what restoring would do ------------------------------------------------
@pytest.mark.django_db
def test_a_hidden_target_carries_a_restore_with_its_path_label_and_consequences(client, auth):
    from listings.moderation import hide_listing
    live = listing(person())
    hide_listing(live, AccountFactory())
    story = _story()
    for target_type, pk, label, path in (("listing", live.pk, "Restore listing", f"/listings/{live.pk}/restore"),
                                         ("story", story.pk, "Restore story", f"/stories/{story.pk}/restore")):
        flag = _flag(target_type, pk, "actioned")
        restore = client.get(f"/admin-api/flags/{flag.flag_id}", **auth).json()["target"]["restore"]
        assert restore["label"] == label and restore["path"] == path and restore["consequences"]
    assert "stay closed" in " ".join(client.get(f"/admin-api/flags/{_flag('listing', live.pk).flag_id}", **auth)
                                     .json()["target"]["restore"]["consequences"])


@pytest.mark.django_db
def test_a_live_target_or_a_type_with_no_restore_has_none(client, auth):
    live = listing(person())
    story = _story(StoryStatus.PUBLISHED)
    owner = AccountFactory()
    for target_type, pk in (("listing", live.pk), ("story", story.pk), ("account", owner.pk)):
        flag = _flag(target_type, pk)
        assert client.get(f"/admin-api/flags/{flag.flag_id}", **auth).json()["target"]["restore"] is None


@pytest.mark.django_db
def test_the_restore_path_the_detail_gives_is_a_route_that_works(client, auth):
    """The console POSTs whatever path the server hands it, so it must be a real, working route for each type."""
    from django.contrib.gis.geos import Point

    from listings.moderation import hide_listing
    from sagip.models import StrayReport
    from sagip.moderation import hide_report
    live, story = listing(person()), _story()
    report = StrayReport.objects.create(species="dog", condition="injured", geom=Point(121.05, 14.63, srid=4326))
    hide_listing(live, AccountFactory()), hide_report(report, AccountFactory())
    for target_type, pk in (("listing", live.pk), ("story", story.pk), ("report", report.pk)):
        flag = _flag(target_type, pk, "actioned")
        path = client.get(f"/admin-api/flags/{flag.flag_id}", **auth).json()["target"]["restore"]["path"]
        assert _post(client, auth, f"/admin-api{path}", {"reason": "mistake"}).status_code == 200, (target_type, path)


# ---- steps 4 and 5 together: P3, the scenario that makes them ship as one ------------------------
@pytest.mark.django_db
def test_a_stale_sibling_flag_cannot_hide_a_restored_listing_again(client, auth):
    """Before step 5: three people flag a listing, one is actioned, staff restore it, and a stale tab actioning another
    open flag hid it again and told the poster twice. Now the takedown closed the siblings, so the stale click is a 409."""
    live = listing(person())
    a, b, c = _flag("listing", live.pk), _flag("listing", live.pk), _flag("listing", live.pk)

    done = _post(client, auth, f"/admin-api/flags/{a.flag_id}/action", {"reason_code": "spam_scam"}).json()
    assert done["side_effects"]["flags_resolved"] == "2"

    assert _post(client, auth, f"/admin-api/listings/{live.pk}/restore", {"reason": "mistake"}).status_code == 200
    stale = _post(client, auth, f"/admin-api/flags/{b.flag_id}/action")

    assert stale.status_code == 409 and stale.json()["error"]["code"] == "already_decided"
    live.refresh_from_db()
    assert live.hidden_at is None
    c.refresh_from_db()
    assert c.status == "actioned"
