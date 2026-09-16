import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from common.otp import issue_code


@pytest.mark.django_db
def test_reset_to_the_current_password_is_refused(client):
    acc = AccountFactory(email="same@kupkop.invalid", email_verified_at=timezone.now())
    acc.set_password("Same1234"); acc.save()
    # NOTE: brief specified purpose="password_reset", but VerificationCode.purpose has
    # max_length=10 ("password_reset" is 14 chars) and the OtpPurpose choices (and every
    # production call site in accounts/views.py) use "reset" for this flow. Substituted.
    code = issue_code(acc, channel="email", purpose="reset")
    res = client.post("/api/v1/auth/password/reset",
                      {"email": "same@kupkop.invalid", "code": code, "new_password": "Same1234"},
                      content_type="application/json")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "password_unchanged"
