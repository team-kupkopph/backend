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
    """Append-only by construction. Asserting the ABSENCE of a write path is the only way to
    keep it absent — a log that can be edited proves nothing.

    ⚠️ This originally asserted that NO route mentioned "audit" at all, which was too broad:
    it banned reading the log as well as writing it, and US-W3 legitimately added a read view.
    The property that matters is the absence of a write METHOD, so that is what is checked."""
    import adminapi.urls as urls

    audit_routes = [p for p in urls.urlpatterns if "audit" in str(p.pattern)]
    assert audit_routes, "the scan found no audit route — broken, not clean"
    for route in audit_routes:
        cls = route.callback.cls
        for verb in ("post", "put", "patch", "delete"):
            assert not hasattr(cls, verb), f"{cls.__name__} exposes {verb.upper()} on the audit log"


@pytest.mark.django_db
def test_an_audit_write_failure_never_breaks_the_request(client, auth, monkeypatch):
    """⚠️ Auditing must not take the surface down. A failure to record is a monitoring
    problem; refusing a reviewer's decision because the log write failed turns an
    observability gap into an outage."""
    def boom(*a, **k):
        raise RuntimeError("audit table unavailable")

    monkeypatch.setattr(AdminAuditLog.objects, "create", boom)
    assert client.get("/admin-api/verifications", **auth).status_code == 200


# -- notes_len for the views that take a `reason` (or a note the view used to restate) -------
# ⚠️ These views each did `request._audit_body = {...}` on the DRF Request — a no-op, because
# DRF's Request never forwards attribute sets to the HttpRequest the middleware reads. The
# `reason` views therefore never recorded notes_len at all. The middleware now reads `reason`
# itself; these tests pin the row, not the assignment.
def _decision_row(action):
    rows = AdminAuditLog.objects.filter(action=action)
    assert rows.count() == 1, f"expected exactly one {action} row"
    return rows.get()


PADDED = "  Repeated abuse reports.  "


@pytest.mark.django_db
def test_suspend_and_reinstate_record_the_reason_length_not_the_text(client, auth):
    account = AccountFactory()
    AdminAuditLog.objects.all().delete()
    base = f"/admin-api/members/{account.account_id}"

    assert client.post(f"{base}/suspend", {"reason": PADDED},
                       content_type="application/json", **auth).status_code == 200
    assert client.post(f"{base}/reinstate", {"reason": "Appeal upheld."},
                       content_type="application/json", **auth).status_code == 200

    # Measured as the view reads it — stripped. Whitespace is not a reason.
    suspended = _decision_row("members/{id}/suspend")
    assert suspended.detail["notes_len"] == len(PADDED.strip())
    assert "abuse" not in str(suspended.detail)
    assert _decision_row("members/{id}/reinstate").detail["notes_len"] == len("Appeal upheld.")


@pytest.mark.django_db
def test_closing_a_shift_records_the_reason_length(client, auth):
    from django.utils import timezone

    from volunteer.models import VolunteerShift
    from volunteer.tests.helpers import verified_shelter
    start = timezone.now() + timezone.timedelta(hours=48)
    shift = VolunteerShift.objects.create(shelter_account=verified_shelter(), starts_at=start,
                                          ends_at=start + timezone.timedelta(hours=2), capacity=3,
                                          title="Morning dog walk", city="Marikina")
    AdminAuditLog.objects.all().delete()

    assert client.post(f"/admin-api/shifts/{shift.pk}/close", {"reason": PADDED},
                       content_type="application/json", **auth).status_code == 200

    row = _decision_row("shifts/{id}/close")
    assert row.detail["notes_len"] == len(PADDED.strip())
    assert "abuse" not in str(row.detail)


@pytest.mark.django_db
def test_escalation_partner_add_and_remove_record_the_note_length(client, auth):
    from adminapi.tests.test_shelters import _in_city, _partner_url, make_shelter
    profile = _in_city(make_shelter("Partner Org", verified=True))
    AdminAuditLog.objects.all().delete()

    assert client.post(_partner_url(profile), {"notes": "  Covers Marikina.  "},
                       content_type="application/json", **auth).status_code == 200
    assert client.post(_partner_url(profile, remove=True), {"notes": PADDED},
                       content_type="application/json", **auth).status_code == 200

    added = _decision_row("shelters/{id}/escalation-partner")
    assert added.detail["notes_len"] == len("Covers Marikina.")
    removed = _decision_row("shelters/{id}/escalation-partner/remove")
    assert removed.detail["notes_len"] == len(PADDED.strip())
    assert "abuse" not in str(removed.detail)


@pytest.mark.django_db
def test_unverifying_a_donation_qr_records_the_note_length(client, auth):
    from adminapi.tests.test_shelters import make_shelter
    from shelter.models import DonationQr
    profile = make_shelter("Org", verified=True)
    qr = DonationQr.objects.create(account=profile.account, provider="gcash", account_name="X",
                                   qr_image_url="s3://qr.png", verified=True)
    AdminAuditLog.objects.all().delete()

    assert client.post(f"/admin-api/donation-qrs/{qr.donation_qr_id}/unverify", {"notes": PADDED},
                       content_type="application/json", **auth).status_code == 200

    row = _decision_row("donation-qrs/{id}/unverify")
    assert row.detail["notes_len"] == len(PADDED.strip())
