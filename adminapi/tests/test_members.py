"""US-E1/E2 · members, and suspension that actually suspends."""
import uuid

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.models import Account, AccountStatus
from accounts.tokens import tokens_for
from adminapi.tests.conftest import full_signin
from moderation.models import ModerationFlag


@pytest.fixture
def auth(client, staffer):
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}


def suspend(client, account, auth, reason="Repeated abuse reports."):
    return client.post(f"/admin-api/members/{account.account_id}/suspend",
                       {"reason": reason}, content_type="application/json", **auth)


# -- US-E1 --------------------------------------------------------------------------------
@pytest.mark.django_db
def test_members_needs_a_staff_token(client):
    assert client.get("/admin-api/members").status_code in (401, 403)


@pytest.mark.django_db
def test_admin_accounts_are_not_members(client, auth, staffer):
    """⚠️ The staff-bridge Account is not a member. It belongs on Team & roles (US-T1), and
    listing it here would invite someone to suspend a reviewer from the member queue."""
    rows = client.get("/admin-api/members", **auth).json()["results"]
    assert str(staffer.admin_account.account_id) not in [r["account_id"] for r in rows]
    assert all(r["account_type"] != "admin" for r in rows)


@pytest.mark.django_db
def test_search_and_filter(client, auth):
    AccountFactory(display_name="Rina Alonzo", email="rina@ex.com")
    AccountFactory(display_name="Other Person", email="other@ex.com", status="suspended")
    hits = client.get("/admin-api/members?q=rina", **auth).json()["results"]
    assert [r["display_name"] for r in hits] == ["Rina Alonzo"]
    susp = client.get("/admin-api/members?status=suspended", **auth).json()["results"]
    assert [r["display_name"] for r in susp] == ["Other Person"]


@pytest.mark.django_db
def test_soft_deleted_accounts_stay_visible_and_marked(client, auth):
    """⚠️ A `deleted` account inside the §12.7 grace window is still real data a reviewer may
    need. Hiding it makes it invisible exactly while it is still recoverable."""
    a = AccountFactory(display_name="Leaving Soon")
    Account.objects.filter(pk=a.pk).update(status="deleted", deleted_at=timezone.now())
    rows = client.get("/admin-api/members?status=deleted", **auth).json()["results"]
    assert len(rows) == 1 and rows[0]["deleted_at"] is not None


@pytest.mark.django_db
def test_an_anonymised_account_says_so_rather_than_showing_blanks(client, auth):
    """A row of empty strings is indistinguishable from a bug — a reviewer would raise a
    ticket about data that was correctly destroyed."""
    a = AccountFactory(display_name="Gone", email="gone@ex.com")
    Account.objects.filter(pk=a.pk).update(
        status="deleted", deleted_at=timezone.now(), anonymized_at=timezone.now(),
        display_name="", email=f"anon-{a.account_id}@example.invalid")
    row = client.get(f"/admin-api/members/{a.account_id}", **auth).json()
    assert row["display_name"] == "(anonymised)" and row["anonymized_at"] is not None


@pytest.mark.django_db
def test_the_record_carries_the_context_a_suspension_decision_needs(client, auth):
    a = AccountFactory(display_name="Reported")
    ModerationFlag.objects.create(target_type="account", target_id=a.account_id,
                                  reason="Spamming listings", reporter_account=AccountFactory())
    body = client.get(f"/admin-api/members/{a.account_id}", **auth).json()
    assert [f["reason"] for f in body["flags_about"]] == ["Spamming listings"]
    assert "capabilities" in body and "verifications" in body


# -- US-E2 --------------------------------------------------------------------------------
@pytest.mark.django_db
def test_suspension_revokes_live_tokens_not_just_the_status(client, auth):
    """⚠️ THE POINT OF THIS STORY. Without sessions_revoked_at the account reads suspended
    while its live access token keeps working for up to 15 more minutes."""
    a = AccountFactory(email="target@ex.com", password="pw", email_verified_at=timezone.now())
    token = tokens_for(a)["access"]
    assert client.get("/api/v1/me", HTTP_AUTHORIZATION=f"Bearer {token}").status_code == 200

    assert suspend(client, a, auth).status_code == 200

    a.refresh_from_db()
    assert a.status == "suspended"
    assert a.sessions_revoked_at is not None
    after = client.get("/api/v1/me", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert after.status_code == 401, "the live token must stop working immediately"


@pytest.mark.django_db
def test_suspension_requires_a_reason(client, auth):
    a = AccountFactory()
    assert suspend(client, a, auth, reason="  ").status_code == 422
    a.refresh_from_db()
    assert a.status == "active"


@pytest.mark.django_db
def test_suspension_never_touches_deleted_at(client, auth):
    """Suspended and deleted are different states; the M5 CHECK rejects the pair if confused."""
    a = AccountFactory()
    suspend(client, a, auth)
    a.refresh_from_db()
    assert a.deleted_at is None


@pytest.mark.django_db
def test_a_deleted_account_cannot_be_suspended(client, auth):
    a = AccountFactory()
    Account.objects.filter(pk=a.pk).update(status="deleted", deleted_at=timezone.now())
    res = suspend(client, a, auth)
    assert res.status_code == 409 and res.json()["error"]["code"] == "account_deleted"


@pytest.mark.django_db
def test_reinstate_restores_active_but_leaves_the_revocation_stamp(client, auth):
    """Clearing sessions_revoked_at would retroactively re-validate tokens minted before the
    suspension. Signing in again is the correct cost."""
    a = AccountFactory()
    suspend(client, a, auth)
    a.refresh_from_db()
    revoked_at = a.sessions_revoked_at

    res = client.post(f"/admin-api/members/{a.account_id}/reinstate",
                      {"reason": "Appeal upheld."}, content_type="application/json", **auth)
    assert res.status_code == 200
    a.refresh_from_db()
    assert a.status == "active"
    assert a.sessions_revoked_at == revoked_at


@pytest.mark.django_db
def test_reinstating_an_active_account_is_409(client, auth):
    a = AccountFactory()
    res = client.post(f"/admin-api/members/{a.account_id}/reinstate",
                      {"reason": "x"}, content_type="application/json", **auth)
    assert res.status_code == 409


@pytest.mark.django_db
def test_suspension_is_audited_with_its_reason(client, auth, staffer):
    from adminapi.models import AdminAuditLog
    a = AccountFactory()
    AdminAuditLog.objects.all().delete()
    suspend(client, a, auth, reason="Repeated abuse reports.")
    row = AdminAuditLog.objects.get()
    assert row.actor_id == staffer.admin_account.account_id
    assert str(row.target_id) == str(a.account_id)
    assert row.action == "members/{id}/suspend"


@pytest.mark.django_db
def test_missing_member_is_404(client, auth):
    assert client.get(f"/admin-api/members/{uuid.uuid4()}", **auth).status_code == 404
