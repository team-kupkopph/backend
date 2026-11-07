"""Review step 12 (MO10, V8) · the member record shows everything flagged about a person's content, what was taken down lately, who has
warned them; and a moderator can warn short of taking anything down."""
import pytest

from accounts.factories import AccountFactory
from accounts.models import AccountStatus
from adminapi.models import AdminAuditLog
from adminapi.tests.conftest import full_signin
from community.models import StoryPost
from listings.tests.adoption_helpers import listing, person
from moderation import notices
from moderation.actions import resolve_flag
from moderation.models import FlagStatus, ModerationFlag, ModerationWarning
from notifications.models import Notification


@pytest.fixture
def auth(client, staffer):
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}


def _flag(target_type, target_id):
    return ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type=target_type, target_id=target_id, reason="x")


def _detail(client, auth, account):
    return client.get(f"/admin-api/members/{account.pk}", **auth).json()


def _warn(client, auth, account, **body):
    return client.post(f"/admin-api/members/{account.pk}/warn", body, content_type="application/json", **auth)


@pytest.mark.django_db
def test_a_person_with_removed_listings_no_longer_looks_clean(client, auth):
    poster = person()
    for _ in range(2):
        resolve_flag(_flag("listing", listing(poster).pk), AccountFactory(), FlagStatus.ACTIONED, reason_code="spam_scam")
    story = StoryPost.objects.create(author_account=poster, story_type="general", caption="x")
    _flag("story", story.pk)
    body = _detail(client, auth, poster)
    assert body["flags_about"] == []                                                 # the old view: nothing about the ACCOUNT
    assert sorted((f["target_type"], f["status"]) for f in body["content_flags"]) == [
        ("listing", "actioned"), ("listing", "actioned"), ("story", "open")]
    assert body["takedowns_90d"] == 2 and body["takedown_threshold"] == 3


@pytest.mark.django_db
def test_somebody_elses_flags_are_not_on_their_record(client, auth):
    other = person()
    _flag("listing", listing(other).pk)
    assert _detail(client, auth, person())["content_flags"] == []


@pytest.mark.django_db
def test_a_system_flag_says_which_rule_raised_it_in_the_queue(client, auth):
    poster = person()
    for _ in range(3):
        resolve_flag(_flag("listing", listing(poster).pk), AccountFactory(), FlagStatus.ACTIONED, reason_code="spam_scam")
    rows = client.get("/admin-api/flags", **auth).json()["results"]
    system = [r for r in rows if r["rule"]]
    assert len(system) == 1 and system[0]["reporter"] is None and system[0]["rule_label"] == "Repeat takedowns"
    assert all(r["rule_label"] is None for r in rows if not r["rule"])               # a person's report has no rule


# ---- Warn ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_warning_tells_the_member_the_rule_and_is_recorded_without_touching_their_account(client, auth):
    member = person()
    res = _warn(client, auth, member, reason_code="harassment", note="internal: second chat complaint")
    assert res.status_code == 201
    assert [w["reason_code"] for w in res.json()["warnings"]] == ["harassment"]
    row = ModerationWarning.objects.get()
    assert row.account_id == member.pk and row.note == "internal: second chat complaint" and row.issued_by is not None
    note = Notification.objects.get(account=member, type="account_warned")
    assert notices.REMOVAL_REASONS["harassment"] in note.body and notices.appeal_line() in note.body
    assert "internal" not in note.body and "second chat" not in note.body
    member.refresh_from_db()
    assert member.status == AccountStatus.ACTIVE


@pytest.mark.django_db
def test_a_warning_needs_a_known_reason_and_something_else_needs_a_note(client, auth):
    member = person()
    assert _warn(client, auth, member).json()["error"]["code"] == "reason_required"
    assert _warn(client, auth, member, reason_code="nope").json()["error"]["code"] == "invalid_reason_code"
    assert _warn(client, auth, member, reason_code="other").json()["error"]["code"] == "note_required"
    assert _warn(client, auth, member, reason_code="spam_scam", note="x" * 2001).json()["error"]["code"] == "note_too_long"
    assert ModerationWarning.objects.count() == 0 and not Notification.objects.filter(account=member).exists()


@pytest.mark.django_db
def test_a_warning_cannot_go_to_staff_a_deleted_or_missing_account_and_needs_a_staff_token(client, auth, staffer):
    import uuid
    assert _warn(client, auth, staffer.admin_account, reason_code="spam_scam").status_code == 404
    assert client.post(f"/admin-api/members/{uuid.uuid4()}/warn", {"reason_code": "spam_scam"}, content_type="application/json",
                       **auth).status_code == 404
    gone = person()
    gone.status = AccountStatus.DELETED
    gone.deleted_at = gone.created_at
    gone.save(update_fields=["status", "deleted_at"])
    assert _warn(client, auth, gone, reason_code="spam_scam").status_code == 409
    assert client.post(f"/admin-api/members/{person().pk}/warn", {"reason_code": "spam_scam"}, content_type="application/json").status_code in (401, 403)


@pytest.mark.django_db
def test_the_warning_can_point_at_the_flag_it_came_from_and_is_audited_without_its_words(client, auth):
    member = person()
    flag = _flag("listing", listing(member).pk)
    assert _warn(client, auth, member, reason_code="misleading", note="n", flag_id=str(flag.pk)).status_code == 201
    assert ModerationWarning.objects.get().flag_id == flag.pk
    row = AdminAuditLog.objects.filter(action="members/{id}/warn").first()
    assert row.detail == {"warned": True, "reason_code": "misleading"} and "n" not in str(row.detail.get("note", ""))


@pytest.mark.django_db
def test_warnings_show_on_the_record_newest_first_with_who_issued_them(client, auth):
    member = person()
    _warn(client, auth, member, reason_code="spam_scam")
    _warn(client, auth, member, reason_code="harassment")
    warnings = _detail(client, auth, member)["warnings"]
    assert [w["reason_code"] for w in warnings] == ["harassment", "spam_scam"] and warnings[0]["issued_by"]
