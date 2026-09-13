import pytest

from accounts.models import Account
from verifications.models import VerificationCode


@pytest.mark.django_db
def test_signup_creates_account_settings_and_code_but_no_tokens(client):
    res = client.post("/api/v1/auth/signup", {
        "account_type": "personal", "display_name": "Ana",
        "email": "ana@example.com", "password": "s3cretpass",
    }, content_type="application/json")
    assert res.status_code == 201
    body = res.json()
    assert body["next"] == "verify_email"
    assert "access" not in body and "refresh" not in body
    acc = Account.objects.get(email="ana@example.com")
    assert acc.email_verified_at is None
    assert acc.settings is not None
    assert VerificationCode.objects.filter(account=acc, purpose="signup").exists()


@pytest.mark.django_db
def test_signup_duplicate_email_returns_409(client, django_user_model):
    from django.utils import timezone

    from accounts.factories import AccountFactory
    AccountFactory(email="dupe@example.com", email_verified_at=timezone.now())
    res = client.post("/api/v1/auth/signup", {
        "account_type": "personal", "display_name": "X",
        "email": "DUPE@example.com", "password": "s3cretpass",
    }, content_type="application/json")
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "email_taken"


@pytest.mark.django_db
def test_signup_with_own_unverified_email_resumes_verification(client):
    """Back out of the OTP screen, submit signup again: the account already exists but is
    the person's own and unverified. Dead-ending with `email_taken` strands them — there is
    no way back into verification. Mirror login's unverified branch instead: re-issue the
    signup code and signal the client to resume, not to go log in."""
    from accounts.factories import AccountFactory
    from verifications.models import VerificationCode

    acc = AccountFactory(email="resume@example.com")  # unverified
    assert not VerificationCode.objects.filter(account=acc).exists()

    res = client.post("/api/v1/auth/signup", {
        "account_type": "personal", "display_name": "Resume Me",
        "email": "RESUME@example.com", "password": "s3cretpass",
    }, content_type="application/json")

    assert res.status_code == 409
    assert res.json()["error"]["code"] == "email_unverified"
    assert VerificationCode.objects.filter(account=acc, purpose="signup").exists()


@pytest.mark.django_db
def test_signup_with_an_already_verified_email_still_says_taken(client):
    """A verified account is someone who finished signing up — possibly a different person.
    That case keeps the generic `email_taken` (§12.1 lets signup reveal existence, nothing
    more) and no code is issued."""
    from django.utils import timezone

    from accounts.factories import AccountFactory
    from verifications.models import VerificationCode

    acc = AccountFactory(email="done@example.com", email_verified_at=timezone.now())

    res = client.post("/api/v1/auth/signup", {
        "account_type": "personal", "display_name": "X",
        "email": "done@example.com", "password": "s3cretpass",
    }, content_type="application/json")

    assert res.status_code == 409
    assert res.json()["error"]["code"] == "email_taken"
    assert not VerificationCode.objects.filter(account=acc).exists()


@pytest.mark.django_db
def test_signup_malformed_email_returns_400(client):
    res = client.post("/api/v1/auth/signup", {
        "account_type": "personal", "display_name": "Ana",
        "email": "not-an-email", "password": "s3cretpass",
    }, content_type="application/json")
    assert res.status_code == 400
    assert res.json()["error"]["code"] != "email_taken"


@pytest.mark.django_db
def test_signup_admin_account_type_rejected(client):
    res = client.post("/api/v1/auth/signup", {
        "account_type": "admin", "display_name": "Evil",
        "email": "evil@example.com", "password": "s3cretpass",
    }, content_type="application/json")
    assert res.status_code == 400
    assert not Account.objects.filter(email="evil@example.com").exists()


@pytest.mark.django_db
def test_signup_shelter_account_type_still_allowed(client):
    res = client.post("/api/v1/auth/signup", {
        "account_type": "shelter", "display_name": "Shelter Co",
        "email": "shelter@example.com", "password": "s3cretpass",
    }, content_type="application/json")
    assert res.status_code == 201
    acc = Account.objects.get(email="shelter@example.com")
    assert acc.account_type == "shelter"


@pytest.mark.django_db
def test_signup_rejects_password_without_a_number(client):
    """Canonical rule (onboarding-validation §signup): min 8 chars, at least one number.
    'password' is 8 chars but has no digit — must be rejected, not accepted."""
    res = client.post("/api/v1/auth/signup", {
        "account_type": "personal", "display_name": "Ana",
        "email": "nonum@example.com", "password": "password",
    }, content_type="application/json")
    assert res.status_code == 400
    assert not Account.objects.filter(email="nonum@example.com").exists()


@pytest.mark.django_db
def test_signup_rejects_short_password(client):
    res = client.post("/api/v1/auth/signup", {
        "account_type": "personal", "display_name": "Ana",
        "email": "short@example.com", "password": "ab1",
    }, content_type="application/json")
    assert res.status_code == 400


@pytest.mark.django_db
def test_signup_accepts_password_with_a_number(client):
    res = client.post("/api/v1/auth/signup", {
        "account_type": "personal", "display_name": "Ana",
        "email": "ok@example.com", "password": "s3cretpass",
    }, content_type="application/json")
    assert res.status_code == 201
