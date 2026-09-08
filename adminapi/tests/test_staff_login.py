"""US-B1 · the two-step staff sign-in."""
import pytest
from django.contrib.auth.models import User
from django_otp.plugins.otp_totp.models import TOTPDevice

from adminapi.tests.conftest import PASSWORD, current_code, full_signin, login


@pytest.mark.django_db
def test_password_alone_mints_no_token(client, staffer):
    """The whole point of two steps. A phished password must reach nothing."""
    res = login(client, staffer.username)
    assert res.status_code == 200
    body = res.json()
    assert body["otp_required"] is True and body["challenge"]
    assert "access" not in body and "refresh" not in body


@pytest.mark.django_db
def test_correct_code_completes_sign_in(client, staffer):
    body = full_signin(client, staffer)
    assert body["access"] and body["refresh"]


@pytest.mark.django_db
def test_wrong_code_is_refused(client, staffer):
    challenge = login(client, staffer.username).json()["challenge"]
    res = client.post("/admin-api/auth/verify-otp",
                      {"challenge": challenge, "code": "000000"},
                      content_type="application/json")
    assert res.status_code == 401 and res.json()["error"]["code"] == "invalid_code"


@pytest.mark.django_db
def test_a_used_code_cannot_be_replayed(client, staffer):
    """django_otp records `last_t`; a second use of the same code must fail."""
    code = current_code(staffer.totp_device)
    c1 = login(client, staffer.username).json()["challenge"]
    first = client.post("/admin-api/auth/verify-otp", {"challenge": c1, "code": code},
                        content_type="application/json")
    assert first.status_code == 200

    c2 = login(client, staffer.username).json()["challenge"]
    second = client.post("/admin-api/auth/verify-otp", {"challenge": c2, "code": code},
                         content_type="application/json")
    assert second.status_code == 401 and second.json()["error"]["code"] == "invalid_code"


@pytest.mark.django_db
def test_a_tampered_challenge_is_refused(client, staffer):
    challenge = login(client, staffer.username).json()["challenge"]
    res = client.post("/admin-api/auth/verify-otp",
                      {"challenge": challenge + "x", "code": current_code(staffer.totp_device)},
                      content_type="application/json")
    assert res.status_code == 401 and res.json()["error"]["code"] == "invalid_challenge"


# -- enumeration asymmetry (§12.1) -------------------------------------------------------
@pytest.mark.django_db
def test_unknown_email_and_wrong_password_are_indistinguishable(client, staffer):
    """Sign-in must not reveal which emails are staff accounts."""
    unknown = login(client, "nobody@kupkopph.com")
    wrong = login(client, staffer.username, password="WRONG")
    assert unknown.status_code == wrong.status_code == 401
    # `request_id` differs per request by design (observability) and carries no information
    # about the account, so it is excluded rather than the comparison being loosened.
    strip = lambda r: {k: v for k, v in r.json()["error"].items() if k != "request_id"}
    assert strip(unknown) == strip(wrong) == {"code": "invalid_credentials"}


@pytest.mark.django_db
def test_a_non_staff_user_cannot_sign_in(client, db):
    """is_staff is the gate. A regular Django user with a password is not a reviewer."""
    User.objects.create_user(username="joe@example.com", email="joe@example.com",
                             password=PASSWORD, is_staff=False)
    res = login(client, "joe@example.com")
    assert res.status_code == 401 and res.json()["error"]["code"] == "invalid_credentials"


# -- the staff bridge --------------------------------------------------------------------
@pytest.mark.django_db
def test_staffer_without_a_staff_profile_is_refused_a_token(client, unbridged_staffer):
    """⚠️ The contract accounts/staff.py states: a missing StaffProfile is a setup error the
    caller must SURFACE, never silently stamp an anonymous review. A token with no
    admin_account_id would let a decision be written with a null reviewed_by."""
    challenge = login(client, unbridged_staffer.username).json()["challenge"]
    res = client.post("/admin-api/auth/verify-otp",
                      {"challenge": challenge,
                       "code": current_code(unbridged_staffer.totp_device)},
                      content_type="application/json")
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "staff_profile_missing"


@pytest.mark.django_db
def test_staffer_without_a_device_is_offered_enrolment_but_NO_token(client, db):
    """Changed deliberately by US-T1: this used to 403 `totp_not_enrolled`, which was a dead
    end — a newly created staffer could never sign in. It now returns an enrolment challenge.

    ⚠️ The security property is unchanged and is what this test guards: no token is issued.
    A correct password still reaches nothing on its own."""
    user = User.objects.create_user(username="nodev@kupkopph.com", email="nodev@kupkopph.com",
                                    password=PASSWORD, is_staff=True)
    res = login(client, user.username)
    assert res.status_code == 200
    body = res.json()
    assert body["enrolment_required"] is True
    assert "access" not in body and "refresh" not in body


@pytest.mark.django_db
def test_an_unconfirmed_device_does_not_count(client, db):
    """An interrupted enrolment must not stand in for a second factor — the staffer is sent
    back to enrolment, and still gets no token."""
    user = User.objects.create_user(username="half@kupkopph.com", email="half@kupkopph.com",
                                    password=PASSWORD, is_staff=True)
    TOTPDevice.objects.create(user=user, name="default", confirmed=False)
    body = login(client, user.username).json()
    assert body["enrolment_required"] is True
    assert "access" not in body


@pytest.mark.django_db
def test_the_token_carries_both_identities(client, staffer):
    from rest_framework_simplejwt.tokens import AccessToken
    token = AccessToken(full_signin(client, staffer)["access"])
    assert token["staff_user_id"] == staffer.id
    assert token["admin_account_id"] == str(staffer.admin_account.account_id)
