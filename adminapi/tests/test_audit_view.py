"""US-W3 · reading the audit log."""
import pytest
from django.contrib.auth.models import Group

from adminapi.models import AdminAuditLog
from adminapi.tests.conftest import full_signin, login


@pytest.fixture
def auth(client, staffer):
    staffer.groups.add(Group.objects.get(name="superadmin"))
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}


@pytest.mark.django_db
def test_a_reviewer_cannot_read_the_audit_log(client, staffer):
    staffer.groups.add(Group.objects.get(name="reviewer"))
    token = full_signin(client, staffer)["access"]
    assert client.get("/admin-api/audit", HTTP_AUTHORIZATION=f"Bearer {token}").status_code == 403


@pytest.mark.django_db
def test_newest_first(client, auth):
    """An audit log is read backwards from 'what just happened', unlike the review queues."""
    rows = client.get("/admin-api/audit", **auth).json()["results"]
    assert len(rows) >= 2
    stamps = [r["created_at"] for r in rows]
    assert stamps == sorted(stamps, reverse=True)


@pytest.mark.django_db
def test_a_failed_login_is_findable_by_the_address_that_was_tried(client, auth):
    """⚠️ Those rows have NO actor, so an actor-only filter would hide exactly the events a
    security review most wants."""
    login(client, "attacker@example.com", password="wrong")
    rows = client.get("/admin-api/audit?actor=attacker@example.com", **auth).json()["results"]
    assert rows and rows[0]["actor_id"] is None
    assert rows[0]["actor_label"] == "attacker@example.com"


@pytest.mark.django_db
def test_filtering_to_errors_only(client, auth):
    client.get("/admin-api/verifications?status=nonsense", **auth)   # a 400
    rows = client.get("/admin-api/audit?outcome=errors", **auth).json()["results"]
    assert rows and all(r["outcome"] != "ok" for r in rows)


@pytest.mark.django_db
def test_filtering_by_action(client, auth):
    client.get("/admin-api/members", **auth)
    rows = client.get("/admin-api/audit?action=members", **auth).json()["results"]
    assert rows and all("members" in r["action"] for r in rows)


@pytest.mark.django_db
def test_a_bad_date_is_a_400_not_a_silent_empty_list(client, auth):
    res = client.get("/admin-api/audit?from=not-a-date", **auth)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_from"


@pytest.mark.django_db
def test_the_log_exposes_no_write_route():
    import adminapi.urls as urls
    from adminapi.audit_views import AuditLogView
    assert any(str(p.pattern) == "audit" for p in urls.urlpatterns)
    for verb in ("post", "put", "patch", "delete"):
        assert not hasattr(AuditLogView, verb), f"AuditLogView exposes {verb.upper()}"
