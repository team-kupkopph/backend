import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.models import Account
from accounts.tokens import tokens_for
from common.otp import _hash
from verifications.models import VerificationCode


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


@pytest.mark.django_db
def test_request_phone_stores_unverified_number_and_issues_sms_code(client):
    acc = AccountFactory(email_verified_at=timezone.now())
    res = client.post("/api/v1/me/phone", {"phone": "+639171234567"},
                      content_type="application/json", **_hdr(acc))
    assert res.status_code == 202
    acc.refresh_from_db()
    assert acc.phone == "+639171234567" and acc.phone_verified_at is None
    row = VerificationCode.objects.get(account=acc, purpose="phone")
    assert row.channel == "sms"


@pytest.mark.django_db
def test_verify_phone_sets_verified_at(client):
    acc = AccountFactory(email_verified_at=timezone.now())
    client.post("/api/v1/me/phone", {"phone": "+639171234567"},
                content_type="application/json", **_hdr(acc))
    # read the code straight from the row (dev-parity with the console sender)
    row = VerificationCode.objects.get(account=acc, purpose="phone")
    # brute a known code by re-issuing with a deterministic value isn't possible;
    # instead assert the wrong-code path, then the right one via hash match.
    good = "654321"
    row.code_hash = _hash(good)
    row.save(update_fields=["code_hash"])
    res = client.post("/api/v1/me/phone/verify", {"code": good},
                      content_type="application/json", **_hdr(acc))
    assert res.status_code == 200
    acc.refresh_from_db()
    assert acc.phone_verified_at is not None


@pytest.mark.django_db
def test_request_phone_taken_by_another_account_is_generic_409(client):
    Account.objects.create_account(account_type="personal", email="a@example.com",
                                   display_name="A", password="s3cretpass")
    other = Account.objects.get(email="a@example.com")
    other.phone = "+639170000000"
    other.save(update_fields=["phone"])
    acc = AccountFactory(email_verified_at=timezone.now())
    res = client.post("/api/v1/me/phone", {"phone": "+639170000000"},
                      content_type="application/json", **_hdr(acc))
    assert res.status_code == 409 and res.json()["error"]["code"] == "phone_taken"


# ---------- F12 · normalise to E.164 before storing / before the uniqueness check ----------

@pytest.mark.django_db
def test_request_phone_rejects_landline_with_field_error_and_issues_no_code(client):
    acc = AccountFactory(email_verified_at=timezone.now())
    res = client.post("/api/v1/me/phone", {"phone": "0281234567"},
                      content_type="application/json", **_hdr(acc))
    assert res.status_code == 400
    err = res.json()["error"]
    assert err["code"] == "invalid" and err["field"] == "phone"
    assert err["message"] == "Enter a Philippine mobile number, e.g. 0917 123 4567"
    acc.refresh_from_db()
    assert acc.phone is None
    assert not VerificationCode.objects.filter(account=acc, purpose="phone").exists()


@pytest.mark.django_db
def test_request_phone_stores_local_spelling_as_e164(client):
    acc = AccountFactory(email_verified_at=timezone.now())
    res = client.post("/api/v1/me/phone", {"phone": "0917 123 4567"},
                      content_type="application/json", **_hdr(acc))
    assert res.status_code == 202
    acc.refresh_from_db()
    assert acc.phone == "+639171234567"


@pytest.mark.django_db
def test_request_phone_collision_is_checked_on_the_normalised_number(client):
    other = AccountFactory()
    other.phone = "+639170000000"
    other.save(update_fields=["phone"])
    acc = AccountFactory(email_verified_at=timezone.now())
    res = client.post("/api/v1/me/phone", {"phone": "0917 000 0000"},
                      content_type="application/json", **_hdr(acc))
    assert res.status_code == 409 and res.json()["error"]["code"] == "phone_taken"
