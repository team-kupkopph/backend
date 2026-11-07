"""Review steps 8 and 9 (MO8, MO7) · a takedown says why, and a story's author is told.

MO8: Sprint 10 US-M2 specified a reason on action and dismiss; the console sent none, the server ignored one and no column existed. The
poster's notice said only "removed", and a restore demanded a reason where the takedown that hurt someone did not. Now ACTIONING a
takedown needs a category (the poster is told it as a fixed sentence) and, for "something else", a note. The note is internal and is shown
to no one outside the console. The reporter's words never reach the poster.

MO7: a story takedown was a silent vanish. The author now hears, with the reason and how to ask for a second look."""
import pytest

from accounts.factories import AccountFactory
from community.models import StoryPost
from listings.tests.adoption_helpers import c, listing, person
from moderation import notices
from moderation.actions import AlreadyDecided, DecisionInvalid, resolve_flag
from moderation.models import FlagStatus, ModerationFlag
from notifications.models import Notification

SECRET_REPORTER_WORDS = "he said the reporter's private grudge"
SECRET_INTERNAL_NOTE = "internal: second strike for this poster"


def _flag(target_type, target_id, reporter=None):
    return ModerationFlag.objects.create(reporter_account=reporter or AccountFactory(), target_type=target_type,
                                         target_id=target_id, reason=SECRET_REPORTER_WORDS)


# ---- step 8 · the reason is required, and kept ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_takedown_without_a_reason_is_refused_and_changes_nothing():
    poster = person()
    live = listing(poster)
    flag = _flag("listing", live.pk)
    with pytest.raises(DecisionInvalid) as err:
        resolve_flag(flag, AccountFactory(), FlagStatus.ACTIONED)
    assert err.value.code == "reason_required"
    flag.refresh_from_db()
    live.refresh_from_db()
    assert flag.status == FlagStatus.OPEN and live.hidden_at is None
    assert not Notification.objects.filter(account=poster).exists()


@pytest.mark.django_db
def test_something_else_needs_a_note_and_an_unknown_reason_is_refused():
    live = listing(person())
    with pytest.raises(DecisionInvalid) as err:
        resolve_flag(_flag("listing", live.pk), AccountFactory(), FlagStatus.ACTIONED, reason_code="other")
    assert err.value.code == "note_required"
    with pytest.raises(DecisionInvalid) as err:
        resolve_flag(_flag("listing", live.pk), AccountFactory(), FlagStatus.ACTIONED, reason_code="whatever")
    assert err.value.code == "invalid_reason_code"
    with pytest.raises(DecisionInvalid) as err:
        resolve_flag(_flag("listing", live.pk), AccountFactory(), FlagStatus.ACTIONED, reason_code="spam_scam", note="x" * 2001)
    assert err.value.code == "note_too_long"


@pytest.mark.django_db
def test_the_reason_and_the_internal_note_are_kept_on_the_flag():
    flag = _flag("listing", listing(person()).pk)
    resolve_flag(flag, AccountFactory(), FlagStatus.ACTIONED, reason_code="harassment", note=f"  {SECRET_INTERNAL_NOTE}  ")
    flag.refresh_from_db()
    assert flag.decision_reason_code == "harassment" and flag.decision_note == SECRET_INTERNAL_NOTE


@pytest.mark.django_db
def test_dismissing_needs_no_reason_and_keeps_an_optional_note_but_no_category():
    flag = _flag("listing", listing(person()).pk)
    resolve_flag(flag, AccountFactory(), FlagStatus.DISMISSED, reason_code="spam_scam", note="not a problem")
    flag.refresh_from_db()
    assert flag.status == FlagStatus.DISMISSED and flag.decision_note == "not a problem" and flag.decision_reason_code == ""


@pytest.mark.django_db
def test_a_record_only_action_and_an_already_held_target_are_not_made_to_give_a_reason():
    """An account flag tells no poster anything; a target already hidden has nothing to explain."""
    resolve_flag(_flag("account", AccountFactory().pk), AccountFactory(), FlagStatus.ACTIONED)
    live = listing(person())
    resolve_flag(_flag("listing", live.pk), AccountFactory(), FlagStatus.ACTIONED, reason_code="spam_scam")
    again = resolve_flag(_flag("listing", live.pk), AccountFactory(), FlagStatus.ACTIONED)[0]     # already hidden: no code needed
    assert again.status == FlagStatus.ACTIONED


@pytest.mark.django_db
def test_siblings_closed_with_the_target_carry_the_same_category():
    live = listing(person())
    main, sibling = _flag("listing", live.pk), _flag("listing", live.pk)
    resolve_flag(main, AccountFactory(), FlagStatus.ACTIONED, reason_code="misleading")
    sibling.refresh_from_db()
    assert sibling.status == FlagStatus.ACTIONED and sibling.decision_reason_code == "misleading"


@pytest.mark.django_db
def test_a_decided_flag_still_cannot_be_decided_again_whatever_reason_comes_with_it():
    flag = _flag("listing", listing(person()).pk)
    resolve_flag(flag, AccountFactory(), FlagStatus.DISMISSED)
    with pytest.raises(AlreadyDecided):
        resolve_flag(flag, AccountFactory(), FlagStatus.ACTIONED, reason_code="spam_scam")


# ---- the poster is told why, and only the safe sentence ---------------------------------------------------------------
@pytest.mark.django_db
@pytest.mark.parametrize("code", list(notices.REMOVAL_REASONS))
def test_the_listing_poster_is_told_the_sentence_for_the_category_chosen(code):
    poster = person()
    resolve_flag(_flag("listing", listing(poster).pk), AccountFactory(), FlagStatus.ACTIONED, reason_code=code, note="n")
    body = Notification.objects.get(account=poster, type="listing_removed").body
    assert notices.REMOVAL_REASONS[code] in body and notices.appeal_line() in body


@pytest.mark.django_db
def test_neither_the_reporters_words_nor_the_internal_note_reach_the_poster():
    poster = person()
    flag = _flag("listing", listing(poster).pk)
    resolve_flag(flag, AccountFactory(), FlagStatus.ACTIONED, reason_code="other", note=SECRET_INTERNAL_NOTE)
    note = Notification.objects.get(account=poster, type="listing_removed")
    for secret in (SECRET_REPORTER_WORDS, SECRET_INTERNAL_NOTE, "grudge", "internal"):
        assert secret not in note.body and secret not in note.title


def test_every_reason_category_has_a_sentence_and_an_unknown_one_has_none():
    from moderation.models import ReasonCode
    assert set(notices.REMOVAL_REASONS) == set(ReasonCode.values)
    assert notices.removal_reason("nonsense") == "" and notices.removal_reason("") == ""


# ---- step 9 · a story's author is told ----------------------------------------------------------------------------------
@pytest.mark.django_db
def test_hiding_a_story_tells_its_author_why_and_how_to_ask_for_a_second_look():
    author = person()
    story = StoryPost.objects.create(author_account=author, story_type="general", caption="a caption")
    flag = _flag("story", story.pk)
    resolve_flag(flag, AccountFactory(), FlagStatus.ACTIONED, reason_code="spam_scam", note=SECRET_INTERNAL_NOTE)
    note = Notification.objects.get(account=author, type="story_removed")
    assert note.title == "Your story was hidden" and note.data == {"story_id": str(story.pk)}
    assert notices.REMOVAL_REASONS["spam_scam"] in note.body and notices.appeal_line() in note.body
    assert "still open it" in note.body
    for secret in (SECRET_REPORTER_WORDS, SECRET_INTERNAL_NOTE):
        assert secret not in note.body


@pytest.mark.django_db
def test_a_story_already_hidden_tells_nobody_again():
    author = person()
    story = StoryPost.objects.create(author_account=author, story_type="general", caption="x", status="hidden")
    resolve_flag(_flag("story", story.pk), AccountFactory(), FlagStatus.ACTIONED)
    assert not Notification.objects.filter(account=author, type="story_removed").exists()


@pytest.mark.django_db
def test_the_author_can_still_open_the_hidden_story_with_its_hidden_state():
    author = person()
    story = StoryPost.objects.create(author_account=author, story_type="general", caption="x")
    resolve_flag(_flag("story", story.pk), AccountFactory(), FlagStatus.ACTIONED, reason_code="misleading")
    mine = c(author).get(f"/api/v1/stories/{story.pk}")
    assert mine.status_code == 200 and mine.json()["status"] == "hidden"
    assert c(person()).get(f"/api/v1/stories/{story.pk}").status_code == 404           # nobody else
