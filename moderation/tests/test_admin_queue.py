"""US-M2 · the admin moderation queue — same pattern as VerificationRequestAdmin's queue."""
import pytest

from accounts.factories import AccountFactory
from accounts.models import StaffProfile
from moderation.models import FlagStatus, ModerationFlag

CHANGELIST = "/admin/moderation/moderationflag/"


def _flag(reporter=None, status=FlagStatus.OPEN, **kw):
    defaults = dict(reporter_account=reporter, target_type="listing",
                    target_id="00000000-0000-0000-0000-000000000000", reason="x", status=status)
    defaults.update(kw)
    return ModerationFlag.objects.create(**defaults)


@pytest.mark.django_db
def test_queue_registered_and_reachable(admin_client):
    _flag(reporter=AccountFactory())
    res = admin_client.get(CHANGELIST)
    assert res.status_code == 200


@pytest.mark.django_db
def test_non_staff_cannot_view_the_queue(client, django_user_model):
    user = django_user_model.objects.create_user("bob", password="irrelevant")
    client.force_login(user)
    res = client.get(CHANGELIST)
    assert res.status_code == 302


@pytest.mark.django_db
def test_a_system_raised_flag_renders_as_system_not_a_person(admin_client):
    _flag(reporter=None)
    content = admin_client.get(CHANGELIST).content.decode()
    assert "System" in content


@pytest.mark.django_db
def test_queue_is_read_only_no_add(admin_client):
    res = admin_client.get(CHANGELIST + "add/")
    assert res.status_code == 403


@pytest.mark.django_db
def test_queue_is_read_only_no_delete(admin_client):
    flag = _flag(reporter=AccountFactory())
    res = admin_client.get(f"{CHANGELIST}{flag.pk}/delete/")
    assert res.status_code == 403


@pytest.mark.django_db
def test_mark_actioned_stamps_the_reviewer_and_status(admin_client, admin_user):
    account = AccountFactory(account_type="admin", email=admin_user.email or "rev@kupkop.ph")
    StaffProfile.objects.create(user=admin_user, account=account)
    flag = _flag(reporter=AccountFactory())

    admin_client.post(CHANGELIST, {"action": "mark_actioned", "_selected_action": [str(flag.pk)]})

    flag.refresh_from_db()
    assert flag.status == FlagStatus.ACTIONED
    assert flag.reviewed_by == account
    assert flag.reviewed_at is not None


@pytest.mark.django_db
def test_actioning_a_story_flag_hides_the_story(admin_client, admin_user):
    """US-T3 / D-S6-4 · the lever: an actioned story flag hides the story (not deleted)."""
    from community.models import StoryPost, StoryStatus
    account = AccountFactory(account_type="admin", email=admin_user.email or "rev@kupkop.ph")
    StaffProfile.objects.create(user=admin_user, account=account)
    story = StoryPost.objects.create(author_account=AccountFactory(), story_type="general",
                                     caption="off-topic")
    flag = _flag(reporter=AccountFactory(), target_type="story", target_id=str(story.pk))

    admin_client.post(CHANGELIST, {"action": "mark_actioned", "_selected_action": [str(flag.pk)]})

    story.refresh_from_db(); flag.refresh_from_db()
    assert story.status == StoryStatus.HIDDEN          # hidden, not deleted
    assert StoryPost.objects.filter(pk=story.pk).exists()
    assert flag.status == FlagStatus.ACTIONED          # flag stays as the audit trail


@pytest.mark.django_db
def test_dismissing_a_story_flag_leaves_the_story_published(admin_client, admin_user):
    from community.models import StoryPost, StoryStatus
    account = AccountFactory(account_type="admin", email=admin_user.email or "rev@kupkop.ph")
    StaffProfile.objects.create(user=admin_user, account=account)
    story = StoryPost.objects.create(author_account=AccountFactory(), story_type="general")
    flag = _flag(reporter=AccountFactory(), target_type="story", target_id=str(story.pk))

    admin_client.post(CHANGELIST, {"action": "mark_dismissed", "_selected_action": [str(flag.pk)]})

    story.refresh_from_db()
    assert story.status == StoryStatus.PUBLISHED       # dismiss doesn't hide


@pytest.mark.django_db
def test_mark_dismissed_sets_status(admin_client, admin_user):
    account = AccountFactory(account_type="admin", email=admin_user.email or "rev@kupkop.ph")
    StaffProfile.objects.create(user=admin_user, account=account)
    flag = _flag(reporter=AccountFactory())

    admin_client.post(CHANGELIST, {"action": "mark_dismissed", "_selected_action": [str(flag.pk)]})

    flag.refresh_from_db()
    assert flag.status == FlagStatus.DISMISSED


@pytest.mark.django_db
def test_resolving_without_a_linked_reviewer_account_is_refused_and_does_not_mutate(admin_client):
    # admin_client's default superuser has no StaffProfile — same guard as
    # VerificationRequestAdmin's _reviewer_or_refuse.
    flag = _flag(reporter=AccountFactory())
    admin_client.post(CHANGELIST, {"action": "mark_actioned", "_selected_action": [str(flag.pk)]})
    flag.refresh_from_db()
    assert flag.status == FlagStatus.OPEN


@pytest.mark.django_db
def test_open_flags_sort_before_resolved_ones_regardless_of_age(admin_client):
    # older-first only applies WITHIN a rank — an open flag always outranks a resolved
    # one, however much older the resolved one is (the queue is what's actionable).
    older_resolved = _flag(reporter=AccountFactory(), status=FlagStatus.DISMISSED)
    newer_open = _flag(reporter=AccountFactory(), status=FlagStatus.OPEN)
    content = admin_client.get(CHANGELIST).content.decode()
    assert content.index(str(newer_open.pk)) < content.index(str(older_resolved.pk))
