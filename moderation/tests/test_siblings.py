"""Review step 5 (MO4, MQ4) · a decision is about the target, not the flag. Actioning a target takes it down for
everyone who reported it, so the other open flags about it are closed with it, in the same transaction, and the
moderator is told how many before the click and after. Dismissing and reviewing stay per flag (MQ4): another
reporter may have a different concern."""
import uuid

import pytest

from accounts.factories import AccountFactory
from community.models import StoryPost
from listings.tests.adoption_helpers import listing, person
from moderation import targets
from moderation.actions import resolve_flag
from moderation.models import FlagStatus, FlagTarget, ModerationFlag


def _flag(target_type, pk, status=FlagStatus.OPEN, reason="x"):
    return ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type=target_type, target_id=pk,
                                         reason=reason, status=status)


def _status(flag):
    flag.refresh_from_db()
    return flag.status


@pytest.mark.django_db
def test_actioning_a_flag_closes_the_open_and_reviewed_flags_about_the_same_target_and_nothing_else():
    live, other = listing(person()), listing(person())
    reviewer = AccountFactory()
    first = _flag("listing", live.pk)
    open_, reviewed = _flag("listing", live.pk), _flag("listing", live.pk, FlagStatus.REVIEWED)
    dismissed = _flag("listing", live.pk, FlagStatus.DISMISSED)
    elsewhere = _flag("listing", other.pk)

    flag, effects = resolve_flag(first, reviewer, "actioned", reason_code="spam_scam")

    assert effects == {"listing_hidden": str(live.pk), "flags_resolved": "2"}
    assert flag.status == FlagStatus.ACTIONED
    for sibling in (open_, reviewed):
        sibling.refresh_from_db()
        assert sibling.status == FlagStatus.ACTIONED and sibling.reviewed_by_id == reviewer.pk
        assert sibling.reviewed_at is not None
    assert _status(dismissed) == FlagStatus.DISMISSED          # a closed decision is never rewritten
    assert _status(elsewhere) == FlagStatus.OPEN               # another listing is another concern


@pytest.mark.django_db
def test_the_count_said_before_the_click_is_the_count_closed():
    """The honesty check: a warning that undercounts is worse than none."""
    live = listing(person())
    first = _flag("listing", live.pk)
    _flag("listing", live.pk), _flag("listing", live.pk), _flag("listing", live.pk, FlagStatus.REVIEWED)
    said = targets.describe_target(FlagTarget.LISTING, live.pk, flag_id=first.pk)["on_action"]
    _, effects = resolve_flag(first, AccountFactory(), "actioned", reason_code="spam_scam")
    assert "3 other flags about this will be closed as actioned too" in " ".join(said["consequences"])
    assert effects["flags_resolved"] == "3"


@pytest.mark.django_db
def test_one_other_flag_reads_in_the_singular_and_none_leaves_the_line_out():
    live = listing(person())
    first = _flag("listing", live.pk)
    assert "other" not in " ".join(targets.describe_target("listing", live.pk, flag_id=first.pk)["on_action"]["consequences"])
    _flag("listing", live.pk)
    said = " ".join(targets.describe_target("listing", live.pk, flag_id=first.pk)["on_action"]["consequences"])
    assert "1 other flag about this will be closed as actioned too" in said


@pytest.mark.django_db
@pytest.mark.parametrize("decision", ["dismissed", "reviewed"])
def test_dismissing_or_reviewing_leaves_the_other_flags_alone(decision):
    """MQ4 · a different reporter may have a different concern."""
    live = listing(person())
    first, other = _flag("listing", live.pk), _flag("listing", live.pk)
    _, effects = resolve_flag(first, AccountFactory(), decision)
    assert effects == {} and _status(other) == FlagStatus.OPEN


@pytest.mark.django_db
def test_a_record_only_action_does_not_close_the_others():
    """An account flag takes nothing down, so there is nothing that settles the other reports about it."""
    owner = AccountFactory()
    first, other = _flag("account", owner.pk), _flag("account", owner.pk)
    _, effects = resolve_flag(first, AccountFactory(), "actioned", reason_code="spam_scam")
    assert effects == {} and _status(other) == FlagStatus.OPEN
    said = " ".join(targets.describe_target("account", owner.pk, flag_id=first.pk)["on_action"]["consequences"])
    assert "other flag" not in said


@pytest.mark.django_db
def test_a_flag_left_open_on_an_already_hidden_target_closes_its_siblings_too():
    """Flags that predate this behaviour: the target is already held down, so the rest of its reports are moot."""
    from listings.moderation import hide_listing
    live = listing(person())
    hide_listing(live, AccountFactory())
    first, other = _flag("listing", live.pk), _flag("listing", live.pk)
    said = targets.describe_target("listing", live.pk, flag_id=first.pk)["on_action"]
    assert said["already_done"] is True and "1 other flag about this will be closed" in " ".join(said["consequences"])
    _, effects = resolve_flag(first, AccountFactory(), "actioned", reason_code="spam_scam")
    assert effects == {"flags_resolved": "1"} and _status(other) == FlagStatus.ACTIONED


@pytest.mark.django_db
def test_a_take_down_that_fails_leaves_every_flag_open():
    """One transaction: no flag is closed on the strength of a takedown that did not happen."""
    live = listing(person())
    first, other = _flag("listing", live.pk), _flag("listing", live.pk)

    def boom(obj, reviewer, reason=""):
        raise RuntimeError("takedown failed")
    original = targets.REGISTRY[FlagTarget.LISTING].take_down
    targets.REGISTRY[FlagTarget.LISTING].take_down = boom
    try:
        with pytest.raises(RuntimeError):
            resolve_flag(first, AccountFactory(), "actioned", reason_code="spam_scam")
    finally:
        targets.REGISTRY[FlagTarget.LISTING].take_down = original
    assert _status(first) == FlagStatus.OPEN and _status(other) == FlagStatus.OPEN


@pytest.mark.django_db
def test_stories_and_other_types_work_the_same_way():
    story = StoryPost.objects.create(author_account=AccountFactory(), story_type="general", caption="x")
    first, other = _flag("story", story.pk), _flag("story", story.pk)
    _, effects = resolve_flag(first, AccountFactory(), "actioned", reason_code="spam_scam")
    assert effects == {"story_hidden": str(story.pk), "flags_resolved": "1"} and _status(other) == FlagStatus.ACTIONED


@pytest.mark.django_db
def test_a_flag_about_a_different_target_type_with_the_same_id_is_not_a_sibling():
    shared = uuid.uuid4()
    live = listing(person())
    first = _flag("listing", live.pk)
    stranger = _flag("report", shared)
    resolve_flag(first, AccountFactory(), "actioned", reason_code="spam_scam")
    assert _status(stranger) == FlagStatus.OPEN
