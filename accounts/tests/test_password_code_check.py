"""`POST /auth/password/code/check` · tell the user their code is wrong BEFORE the password.

THE BUG THIS FIXES. The reset flow is three screens: email → code → new password. The code
was only ever checked by `/auth/password/reset`, which needs the new password too — so an
expired or mistyped code was reported only after the user had chosen and typed a new
password, on the screen for the password rather than the screen for the code.
`ResetOtpScreen`'s own comment said as much: "checking it early here would need its own
endpoint, which doesn't exist."

⚠️ WHY THIS IS NOT A NEW BRUTE-FORCE ORACLE. A guess costs an attempt here exactly as it
does at `/auth/password/reset`, against the same `max_attempts` on the same row, and the
endpoint carries its own IP+identifier throttle pair. Anyone who wanted to grind codes could
already do it by posting to /reset with a throwaway password; this changes the ergonomics of
the honest path, not the economics of the dishonest one.
"""
import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from common import otp
from verifications.models import VerificationCode

URL = "/api/v1/auth/password/code/check"


@pytest.mark.django_db
def test_a_correct_code_is_accepted(client):
    acc = AccountFactory(email="c@example.com", email_verified_at=timezone.now())
    code = otp.issue_code(acc, channel="email", purpose="reset")
    res = client.post(URL, {"email": acc.email, "code": code}, content_type="application/json")
    assert res.status_code == 200


@pytest.mark.django_db
def test_checking_does_not_consume_the_code(client):
    """THE ONE THAT MATTERS. If the check consumed the code, the reset that follows it —
    seconds later, with the same code — would fail, and the fix would be worse than the bug."""
    acc = AccountFactory(email="c@example.com", password="oldpass12",
                         email_verified_at=timezone.now())
    code = otp.issue_code(acc, channel="email", purpose="reset")

    assert client.post(URL, {"email": acc.email, "code": code},
                       content_type="application/json").status_code == 200
    assert VerificationCode.objects.get(account=acc, purpose="reset").consumed_at is None

    res = client.post("/api/v1/auth/password/reset",
                      {"email": acc.email, "code": code, "new_password": "newpass12"},
                      content_type="application/json")
    assert res.status_code == 200
    acc.refresh_from_db()
    assert acc.check_password("newpass12")


@pytest.mark.django_db
def test_a_correct_check_costs_no_attempt(client):
    acc = AccountFactory(email="c@example.com", email_verified_at=timezone.now())
    code = otp.issue_code(acc, channel="email", purpose="reset")
    client.post(URL, {"email": acc.email, "code": code}, content_type="application/json")
    assert VerificationCode.objects.get(account=acc, purpose="reset").attempts == 0


@pytest.mark.django_db
def test_a_wrong_code_costs_an_attempt_exactly_as_reset_does(client):
    # Same accounting as the real endpoint — that equivalence is what keeps this from being
    # a cheaper way to guess.
    acc = AccountFactory(email="c@example.com", email_verified_at=timezone.now())
    otp.issue_code(acc, channel="email", purpose="reset")
    res = client.post(URL, {"email": acc.email, "code": "000000"},
                      content_type="application/json")
    assert res.status_code == 400
    assert VerificationCode.objects.get(account=acc, purpose="reset").attempts == 1


@pytest.mark.django_db
def test_an_expired_code_is_reported_as_expired_not_invalid(client):
    # The user needs to know to press Resend, not to retype what they already typed right.
    acc = AccountFactory(email="c@example.com", email_verified_at=timezone.now())
    code = otp.issue_code(acc, channel="email", purpose="reset")
    row = VerificationCode.objects.get(account=acc, purpose="reset")
    row.expires_at = timezone.now() - timezone.timedelta(seconds=1)
    row.save(update_fields=["expires_at"])
    res = client.post(URL, {"email": acc.email, "code": code}, content_type="application/json")
    # 410, not 400 — the flow needs to tell "retype it" apart from "press Resend".
    assert res.status_code == 410
    assert res.json()["error"]["code"] == "code_expired"


@pytest.mark.django_db
def test_an_unknown_account_answers_exactly_like_a_wrong_code(client):
    """§12.1 · the check must not become the account-enumeration oracle that /reset refuses
    to be. Compared field by field against the real endpoint's answer, not eyeballed."""
    acc = AccountFactory(email="real@example.com", email_verified_at=timezone.now())
    otp.issue_code(acc, channel="email", purpose="reset")

    unknown = client.post(URL, {"email": "nobody@example.com", "code": "000000"},
                          content_type="application/json")
    known_wrong = client.post(URL, {"email": acc.email, "code": "000000"},
                              content_type="application/json")

    def without_correlation_id(response):
        # `request_id` is per-request by design (US-E2) and carries nothing about the account.
        body = response.json()
        body["error"].pop("request_id", None)
        return body

    assert unknown.status_code == known_wrong.status_code == 400
    assert without_correlation_id(unknown) == without_correlation_id(known_wrong)


@pytest.mark.django_db
def test_running_out_of_attempts_locks_the_code(client):
    acc = AccountFactory(email="c@example.com", email_verified_at=timezone.now())
    otp.issue_code(acc, channel="email", purpose="reset")
    for _ in range(5):
        client.post(URL, {"email": acc.email, "code": "000000"}, content_type="application/json")
    res = client.post(URL, {"email": acc.email, "code": "000000"},
                      content_type="application/json")
    assert res.status_code == 423
    assert res.json()["error"]["code"] == "code_locked"
