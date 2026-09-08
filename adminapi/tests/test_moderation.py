"""US-M1/M2 · the moderation queue, target resolution and decisions."""
import uuid

import pytest

from accounts.factories import AccountFactory
from adminapi.tests.conftest import full_signin
from community.models import StoryPost, StoryStatus
from moderation.models import FlagStatus, ModerationFlag


@pytest.fixture
def auth(client, staffer):
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}


def make_flag(**kw):
    kw.setdefault("target_type", "listing")
    kw.setdefault("target_id", uuid.uuid4())
    kw.setdefault("reason", "Possible spam")
    kw.setdefault("status", "open")
    if "reporter_account" not in kw:
        kw["reporter_account"] = AccountFactory()
    return ModerationFlag.objects.create(**kw)


# -- US-M1 · queue ------------------------------------------------------------------------
@pytest.mark.django_db
def test_the_queue_needs_a_staff_token(client):
    assert client.get("/admin-api/flags").status_code in (401, 403)


@pytest.mark.django_db
def test_defaults_to_open_oldest_first(client, auth):
    make_flag(reason="first")
    make_flag(reason="second")
    make_flag(reason="already handled", status="dismissed")
    rows = client.get("/admin-api/flags", **auth).json()["results"]
    assert [r["reason"] for r in rows] == ["first", "second"]


@pytest.mark.django_db
def test_filters_by_status_and_target_type(client, auth):
    make_flag(target_type="story", reason="a story")
    make_flag(target_type="account", reason="an account")
    stories = client.get("/admin-api/flags?target_type=story", **auth).json()["results"]
    assert [r["reason"] for r in stories] == ["a story"]
    assert client.get("/admin-api/flags?target_type=nope", **auth).status_code == 400


@pytest.mark.django_db
def test_a_system_raised_flag_renders_as_null_reporter_not_a_crash(client, auth):
    """⚠️ reporter_account is nullable by schema accommodation — a system rule can raise a
    flag. The UI shows 'System'; the API must not blow up producing the row."""
    make_flag(reporter_account=None, reason="repeat withdrawals")
    row = client.get("/admin-api/flags", **auth).json()["results"][0]
    assert row["reporter"] is None


# -- US-M1 · target resolution ------------------------------------------------------------
@pytest.mark.django_db
def test_a_present_target_is_described(client, auth):
    account = AccountFactory(display_name="Spammy Org")
    flag = make_flag(target_type="account", target_id=account.account_id)
    target = client.get(f"/admin-api/flags/{flag.flag_id}", **auth).json()["target"]
    assert target["state"] == "present" and target["label"] == "Spammy Org"


@pytest.mark.django_db
def test_a_deleted_target_says_so_and_is_not_a_500(client, auth):
    """⚠️ A missing target is NORMAL — it is often why something was flagged, and moderation
    frequently ends with the target gone."""
    flag = make_flag(target_type="account", target_id=uuid.uuid4())
    res = client.get(f"/admin-api/flags/{flag.flag_id}", **auth)
    assert res.status_code == 200
    assert res.json()["target"]["state"] == "gone"


@pytest.mark.django_db
def test_a_target_type_with_no_model_is_distinct_from_a_deleted_one(client, auth):
    """Messaging is Phase 2 — `message` flags are describable but not resolvable. Reporting
    that as 'gone' would claim something was deleted that never existed."""
    flag = make_flag(target_type="message")
    target = client.get(f"/admin-api/flags/{flag.flag_id}", **auth).json()["target"]
    assert target["state"] == "unsupported"


# -- US-M2 · decisions --------------------------------------------------------------------
@pytest.mark.django_db
def test_dismiss_stamps_the_reviewer(client, auth, staffer):
    flag = make_flag()
    assert client.post(f"/admin-api/flags/{flag.flag_id}/dismiss", {},
                       content_type="application/json", **auth).status_code == 200
    flag.refresh_from_db()
    assert flag.status == "dismissed"
    assert flag.reviewed_by_id == staffer.admin_account.account_id
    assert flag.reviewed_at is not None


@pytest.mark.django_db
def test_actioning_a_story_flag_hides_the_story(client, auth):
    """⚠️ US-T3 / D-S6-4. This behaviour lived ONLY inside moderation/admin.py — the surface
    US-X2 deletes. It now lives in moderation/actions.py and both surfaces call it, so the
    cutover cannot silently take it away."""
    story = StoryPost.objects.create(author_account=AccountFactory(), story_type="adoption",
                                     caption="spam spam")
    flag = make_flag(target_type="story", target_id=story.story_id)

    body = client.post(f"/admin-api/flags/{flag.flag_id}/action", {},
                       content_type="application/json", **auth).json()

    story.refresh_from_db()
    assert story.status == StoryStatus.HIDDEN, "actioning a story flag must hide the story"
    # Hidden, NOT deleted — the row and its photos survive and the author sees a hidden state.
    assert StoryPost.objects.filter(pk=story.story_id).exists()
    # And the reviewer is told, rather than the side effect being invisible.
    assert body["side_effects"]["story_hidden"] == str(story.story_id)


@pytest.mark.django_db
def test_actioning_an_account_flag_does_not_suspend_the_account(client, auth):
    """⚠️ Actioning records the JUDGEMENT. Suspension has its own side effects (token
    revocation, US-E2) and is taken separately, by someone who chose to take it."""
    account = AccountFactory(display_name="Reported Person")
    flag = make_flag(target_type="account", target_id=account.account_id)
    client.post(f"/admin-api/flags/{flag.flag_id}/action", {},
                content_type="application/json", **auth)
    account.refresh_from_db()
    assert account.status == "active"


@pytest.mark.django_db
def test_a_second_decision_is_409(client, auth):
    flag = make_flag()
    assert client.post(f"/admin-api/flags/{flag.flag_id}/dismiss", {},
                       content_type="application/json", **auth).status_code == 200
    res = client.post(f"/admin-api/flags/{flag.flag_id}/action", {},
                      content_type="application/json", **auth)
    assert res.status_code == 409 and res.json()["error"]["code"] == "already_decided"
    flag.refresh_from_db()
    assert flag.status == "dismissed"


@pytest.mark.django_db
def test_reviewed_is_not_terminal(client, auth):
    """`reviewed` means 'looked at', not 'finished' — a moderator must still be able to act."""
    flag = make_flag()
    client.post(f"/admin-api/flags/{flag.flag_id}/review", {}, content_type="application/json", **auth)
    assert client.post(f"/admin-api/flags/{flag.flag_id}/action", {},
                       content_type="application/json", **auth).status_code == 200


@pytest.mark.django_db
def test_dismissing_a_story_flag_leaves_the_story_published(client, auth):
    """Ported from moderation/tests/test_admin_queue.py, which US-X2 deleted.

    The negative of the hiding rule, and the more dangerous direction: a dismissal that hid
    the story would silently punish a post whose flag was found baseless.
    """
    story = StoryPost.objects.create(author_account=AccountFactory(), story_type="adoption",
                                     caption="a fine post")
    flag = make_flag(target_type="story", target_id=story.story_id)

    client.post(f"/admin-api/flags/{flag.flag_id}/dismiss", {},
                content_type="application/json", **auth)
    story.refresh_from_db()
    assert story.status == StoryStatus.PUBLISHED
