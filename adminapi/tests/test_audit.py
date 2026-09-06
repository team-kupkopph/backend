"""US-D1 · every /admin-api/* call is recorded, and the row carries no secrets."""
import pytest

from accounts.factories import AccountFactory
from adminapi.models import AdminAuditLog
from adminapi.tests.conftest import PASSWORD, current_code, full_signin, login
from verifications.models import VerificationRequest


@pytest.fixture
def auth(client, staffer):
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}


@pytest.mark.django_db
def test_every_admin_api_call_is_recorded(client, auth):
    AdminAuditLog.objects.all().delete()
    client.get("/admin-api/verifications", **auth)
    assert AdminAuditLog.objects.count() == 1
    row = AdminAuditLog.objects.get()
    assert row.action == "verifications" and row.outcome == "ok"


@pytest.mark.django_db
def test_a_failure_is_recorded_too(client, auth):
    """A row only on success would leave the audit trail blind to exactly the calls a
    security review cares about."""
    AdminAuditLog.objects.all().delete()
    client.get("/admin-api/verifications?status=nonsense", **auth)
    assert AdminAuditLog.objects.get().outcome == "error_400"


@pytest.mark.django_db
def test_an_unauthenticated_failed_login_is_recorded_and_attributed(client, db):
    """⚠️ The row this table's shape exists for. A failed login has no actor, so a NOT NULL
    actor_id would force the middleware to skip it — and it is the event a security audit
    most needs."""
    AdminAuditLog.objects.all().delete()
    login(client, "attacker@example.com", password="wrong")
    row = AdminAuditLog.objects.get()
    assert row.actor_id is None
    assert row.actor_label == "attacker@example.com", "an action must never be unattributed"
    assert row.outcome.startswith("error_")


@pytest.mark.django_db
def test_the_row_carries_neither_the_password_nor_the_totp_code(client, staffer):
    """detail is an ALLOW-LIST. A deny-list of secrets fails open on the next field added."""
    AdminAuditLog.objects.all().delete()
    res = login(client, staffer.username)
    code = current_code(staffer.totp_device)
    client.post("/admin-api/auth/verify-otp",
                {"challenge": res.json()["challenge"], "code": code},
                content_type="application/json")

    blob = "".join(str(r.detail) + r.actor_label for r in AdminAuditLog.objects.all())
    assert PASSWORD not in blob
    assert code not in blob
    assert "challenge" not in blob


@pytest.mark.django_db
def test_a_decision_records_the_target_and_whether_a_reason_was_given(client, auth, staffer):
    account = AccountFactory(display_name="Org", email="org@ex.com")
    vr = VerificationRequest.objects.create(account=account, type="shelter_org", status="pending")
    AdminAuditLog.objects.all().delete()

    client.post(f"/admin-api/verifications/{vr.verification_id}/reject",
                {"notes": "Billing proof is illegible."},
                content_type="application/json", **auth)

    row = AdminAuditLog.objects.get()
    assert row.actor_id == staffer.admin_account.account_id
    assert str(row.target_id) == str(vr.verification_id)
    assert row.action == "verifications/{id}/reject", "ids belong in target_id, not the action"
    # The LENGTH of the note, never the text — that is the applicant's, and it already lives
    # on the request row.
    assert row.detail["notes_len"] == len("Billing proof is illegible.")
    assert "illegible" not in str(row.detail)


@pytest.mark.django_db
def test_non_admin_api_paths_are_not_recorded(client, db):
    AdminAuditLog.objects.all().delete()
    client.get("/api/v1/health")
    assert AdminAuditLog.objects.count() == 0


@pytest.mark.django_db
def test_the_model_exposes_no_update_or_delete_path(client, auth):
    """Append-only by construction. Asserting the ABSENCE of a route is the only way to keep
    it absent — a log that can be edited proves nothing."""
    import adminapi.urls as urls
    paths = [str(p.pattern) for p in urls.urlpatterns]
    assert not any("audit" in p for p in paths), f"an audit-mutating route appeared: {paths}"


@pytest.mark.django_db
def test_an_audit_write_failure_never_breaks_the_request(client, auth, monkeypatch):
    """⚠️ Auditing must not take the surface down. A failure to record is a monitoring
    problem; refusing a reviewer's decision because the log write failed turns an
    observability gap into an outage."""
    def boom(*a, **k):
        raise RuntimeError("audit table unavailable")

    monkeypatch.setattr(AdminAuditLog.objects, "create", boom)
    assert client.get("/admin-api/verifications", **auth).status_code == 200
