"""NT-9 / F13 · staff revoke a member's badge from the console."""
import pytest

from accounts.factories import AccountFactory
from adminapi.models import AdminAuditLog
from adminapi.tests.conftest import full_signin
from community.badges import award_badges_for
from community.models import AccountBadge, BadgeRevocation
from community.tests.test_badges_safe_award import _completed_shift


@pytest.fixture
def auth(client, staffer):
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}


def revoke(client, account, auth, code="first_shift", reason="Shifts were fabricated."):
    body = {"reason": reason} if reason is not None else {}
    return client.post(f"/admin-api/members/{account.account_id}/badges/{code}/revoke", body, content_type="application/json", **auth)


def _holder():
    account = AccountFactory()
    _completed_shift(account)
    award_badges_for(account)
    return account


@pytest.mark.django_db
def test_a_member_detail_lists_their_badges(client, auth):
    holder = _holder()
    detail = client.get(f"/admin-api/members/{holder.account_id}", **auth).json()
    assert [b["badge_code"] for b in detail["badges"]] == ["first_shift"]
    assert detail["revoked_badges"] == []


@pytest.mark.django_db
def test_revoke_needs_a_staff_token(client):
    holder = _holder()
    res = client.post(f"/admin-api/members/{holder.account_id}/badges/first_shift/revoke", {"reason": "x"}, content_type="application/json")
    assert res.status_code in (401, 403)
    assert AccountBadge.objects.filter(account=holder).exists()


@pytest.mark.django_db
def test_revoke_needs_a_reason(client, auth):
    holder = _holder()
    for reason in (None, "", "   "):
        res = revoke(client, holder, auth, reason=reason)
        assert res.status_code == 422 and res.json()["error"]["code"] == "reason_required"
    assert AccountBadge.objects.filter(account=holder).exists()


@pytest.mark.django_db
def test_revoke_takes_the_badge_records_who_and_why_and_is_audited(client, auth, staffer):
    holder = _holder()
    res = revoke(client, holder, auth)
    assert res.status_code == 200
    body = res.json()
    assert body["badges"] == [] and [b["badge_code"] for b in body["revoked_badges"]] == ["first_shift"]
    row = BadgeRevocation.objects.get(account=holder, badge_id="first_shift")
    assert row.reason == "Shifts were fabricated." and row.revoked_by == staffer.admin_account
    log = AdminAuditLog.objects.get(action="members/{id}/badges/first_shift/revoke")
    assert str(log.target_id) == str(holder.account_id) and log.detail == {"notes_len": len("Shifts were fabricated.")}


@pytest.mark.django_db
def test_revoking_a_badge_not_held_is_a_conflict_and_changes_nothing(client, auth):
    stranger = AccountFactory()
    res = revoke(client, stranger, auth)
    assert res.status_code == 409 and res.json()["error"]["code"] == "not_held"
    assert not BadgeRevocation.objects.exists()


@pytest.mark.django_db
def test_an_unknown_member_or_badge_is_not_found(client, auth):
    import uuid
    ghost = type("G", (), {"account_id": uuid.uuid4()})()
    assert revoke(client, ghost, auth).status_code == 404
    assert revoke(client, _holder(), auth, code="no_such_badge").status_code == 404
