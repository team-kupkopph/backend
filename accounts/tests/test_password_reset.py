import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from common import otp


@pytest.mark.django_db
def test_forgot_is_generic_for_unknown(client):
    res = client.post("/api/v1/auth/password/forgot",
                      {"email": "nobody@example.com"}, content_type="application/json")
    assert res.status_code == 200 and res.json() == {}


@pytest.mark.django_db
def test_reset_with_code_changes_password_and_revokes_sessions(client):
    acc = AccountFactory(email="r@example.com", password="oldpass12",
                         email_verified_at=timezone.now())
    code = otp.issue_code(acc, channel="email", purpose="reset")
    res = client.post("/api/v1/auth/password/reset",
                      {"email": acc.email, "code": code, "new_password": "newpass12"},
                      content_type="application/json")
    assert res.status_code == 200
    acc.refresh_from_db()
    assert acc.check_password("newpass12")
    assert acc.sessions_revoked_at is not None   # reset revokes existing sessions
