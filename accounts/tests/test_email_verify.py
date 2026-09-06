import pytest

from accounts.factories import AccountFactory
from common import otp


def _signup_code(email="v@example.com"):
    acc = AccountFactory(email=email)
    code = otp.issue_code(acc, channel="email", purpose="signup")
    return acc, code


@pytest.mark.django_db
def test_verify_correct_code_verifies_email_and_returns_tokens(client):
    acc, code = _signup_code()
    res = client.post("/api/v1/auth/email/verify",
                      {"email": acc.email, "code": code}, content_type="application/json")
    assert res.status_code == 200
    body = res.json()
    assert body["access"] and body["refresh"]
    assert body["account"]["email"] == acc.email
    acc.refresh_from_db()
    assert acc.email_verified_at is not None


@pytest.mark.django_db
def test_verify_wrong_code_returns_400_with_attempts_left(client):
    acc, _ = _signup_code("w@example.com")
    res = client.post("/api/v1/auth/email/verify",
                      {"email": acc.email, "code": "000000"}, content_type="application/json")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "code_invalid"
    assert res.json()["error"]["details"]["attempts_left"] == 4


@pytest.mark.django_db
def test_verify_unknown_email_looks_like_a_wrong_code(client):
    res = client.post("/api/v1/auth/email/verify",
                      {"email": "never-registered@example.com", "code": "123456"},
                      content_type="application/json")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "code_invalid"
    assert res.json()["error"]["details"]["attempts_left"] == 4


@pytest.mark.django_db
def test_resend_is_generic_even_for_unknown_email(client):
    res = client.post("/api/v1/auth/email/resend",
                      {"email": "nobody@example.com"}, content_type="application/json")
    assert res.status_code == 202
    assert res.json() == {}
