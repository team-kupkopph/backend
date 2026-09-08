"""US-T1 · team, roles, TOTP enrolment and the lockout guards."""
import pytest
from django.contrib.auth.models import Group, User
from django_otp.plugins.otp_totp.models import TOTPDevice

from accounts.models import Account, StaffProfile
from adminapi.tests.conftest import PASSWORD, current_code, full_signin, login


@pytest.fixture
def superadmin(client, staffer):
    staffer.groups.add(Group.objects.get(name="superadmin"))
    return staffer


@pytest.fixture
def auth(client, superadmin):
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, superadmin)['access']}"}


def create_staff(client, auth, email="new@kupkopph.com", role="reviewer"):
    return client.post("/admin-api/staff",
                       {"email": email, "display_name": "New Person", "role": role},
                       content_type="application/json", **auth)


# -- role gating --------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_reviewer_cannot_see_the_team(client, staffer):
    """403, not 404 — the refusal says 'not allowed', not 'does not exist'."""
    staffer.groups.add(Group.objects.get(name="reviewer"))
    token = full_signin(client, staffer)["access"]
    assert client.get("/admin-api/staff", HTTP_AUTHORIZATION=f"Bearer {token}").status_code == 403


# -- creation -----------------------------------------------------------------------------
@pytest.mark.django_db
def test_creating_a_staffer_creates_all_three_rows(client, auth):
    """⚠️ auth.User + Account(admin) + StaffProfile. Any two without the third is the setup
    error US-B1 refuses a token for — a staffer who cannot sign in and no message saying why."""
    res = create_staff(client, auth)
    assert res.status_code == 201

    user = User.objects.get(username="new@kupkopph.com")
    profile = StaffProfile.objects.get(user=user)
    assert profile.account.account_type == "admin"
    assert Account.objects.filter(email="new@kupkopph.com", account_type="admin").exists()
    assert res.json()["staff_profile_linked"] is True
    assert res.json()["totp_enrolled"] is False


@pytest.mark.django_db
def test_a_failed_create_leaves_nothing_behind(client, auth, monkeypatch):
    """The atomic() earning its place: a partial create is the worst outcome, because it looks
    like success and fails only at sign-in."""
    def boom(*a, **k):
        raise RuntimeError("db blip")
    monkeypatch.setattr(StaffProfile.objects, "create", boom)

    with pytest.raises(RuntimeError):
        create_staff(client, auth, email="ghost@kupkopph.com")

    assert not User.objects.filter(username="ghost@kupkopph.com").exists()
    assert not Account.objects.filter(email="ghost@kupkopph.com").exists()


@pytest.mark.django_db
def test_duplicate_email_is_refused(client, auth):
    create_staff(client, auth)
    assert create_staff(client, auth).status_code == 409


# -- the lockout guards -------------------------------------------------------------------
@pytest.mark.django_db
def test_a_superadmin_cannot_demote_themselves(client, auth, superadmin):
    """⚠️ Locking every human out of the only ops surface is unrecoverable from inside the
    product — and after US-X2 there is no Django admin left to recover from."""
    res = client.patch(f"/admin-api/staff/{superadmin.id}", {"role": "reviewer"},
                       content_type="application/json", **auth)
    assert res.status_code == 409 and res.json()["error"]["code"] == "cannot_demote_self"
    assert superadmin.groups.filter(name="superadmin").exists()


@pytest.mark.django_db
def test_a_superadmin_cannot_deactivate_themselves(client, auth, superadmin):
    res = client.patch(f"/admin-api/staff/{superadmin.id}", {"is_active": False},
                       content_type="application/json", **auth)
    assert res.status_code == 409 and res.json()["error"]["code"] == "cannot_deactivate_self"


@pytest.mark.django_db
def test_the_last_superadmin_cannot_be_demoted_by_another(client, auth, superadmin):
    """Two superadmins, each able to demote the other, still ends with zero if they race — so
    the count is checked, not just self-reference."""
    other = User.objects.create_user(username="other@kupkopph.com", email="other@kupkopph.com",
                                     password=PASSWORD, is_staff=True)
    account = Account.objects.create(account_type="admin", email="other@kupkopph.com",
                                     display_name="Other")
    StaffProfile.objects.create(user=other, account=account)
    other.groups.add(Group.objects.get(name="superadmin"))

    # Demoting `other` is fine — the acting superadmin remains.
    assert client.patch(f"/admin-api/staff/{other.id}", {"role": "reviewer"},
                        content_type="application/json", **auth).status_code == 200

    # Now `other` is a reviewer, so the acting user is the last superadmin. Deactivating them
    # from another session must be refused.
    superadmin.groups.clear()
    superadmin.groups.add(Group.objects.get(name="superadmin"))
    other.groups.clear()
    other.groups.add(Group.objects.get(name="superadmin"))
    res = client.patch(f"/admin-api/staff/{superadmin.id}", {"is_active": False},
                       content_type="application/json", **auth)
    assert res.status_code == 409


@pytest.mark.django_db
def test_deactivation_never_deletes_the_row(client, auth):
    """verification_request.reviewed_by and admin_audit_log.actor_id must stay resolvable — a
    decision whose reviewer vanished is an audit trail with a hole in it."""
    created = create_staff(client, auth).json()
    res = client.patch(f"/admin-api/staff/{created['id']}", {"is_active": False},
                       content_type="application/json", **auth)
    assert res.status_code == 200 and res.json()["is_active"] is False
    assert User.objects.filter(id=created["id"]).exists()


# -- TOTP -----------------------------------------------------------------------------------
@pytest.mark.django_db
def test_reset_totp_clears_the_device_without_touching_the_password(client, auth, staffer):
    """⚠️ Device only. One action that also set a password would be a complete account
    takeover in a single superadmin click."""
    before = staffer.password
    res = client.post(f"/admin-api/staff/{staffer.id}/reset-totp", {},
                      content_type="application/json", **auth)
    assert res.status_code == 200 and res.json()["totp_enrolled"] is False
    assert not TOTPDevice.objects.filter(user=staffer).exists()
    staffer.refresh_from_db()
    assert staffer.password == before


# -- first-run enrolment --------------------------------------------------------------------
@pytest.mark.django_db
def test_a_new_staffer_is_offered_enrolment_not_a_dead_end(client, db):
    """⚠️ Without this a newly created staffer can NEVER sign in: US-B1 refuses a login with no
    device, and a superadmin cannot enrol for them because the secret must reach their phone."""
    user = User.objects.create_user(username="fresh@kupkopph.com", email="fresh@kupkopph.com",
                                    password=PASSWORD, is_staff=True)
    account = Account.objects.create(account_type="admin", email="fresh@kupkopph.com",
                                     display_name="Fresh")
    StaffProfile.objects.create(user=user, account=account)

    res = login(client, "fresh@kupkopph.com")
    assert res.status_code == 200
    body = res.json()
    assert body["enrolment_required"] is True and body["challenge"]

    got = client.post("/admin-api/auth/enrol-totp", {"challenge": body["challenge"]},
                      content_type="application/json")
    assert got.status_code == 200
    assert got.json()["provisioning_uri"].startswith("otpauth://totp/")

    device = TOTPDevice.objects.get(user=user)
    assert device.confirmed is False, "an unconfirmed device must not count as a second factor"

    done = client.post("/admin-api/auth/confirm-totp",
                       {"challenge": body["challenge"], "code": current_code(device)},
                       content_type="application/json")
    assert done.status_code == 200 and done.json()["access"]
    device.refresh_from_db()
    assert device.confirmed is True


@pytest.mark.django_db
def test_an_enrolled_staffer_cannot_silently_re_enrol(client, staffer):
    """Otherwise anyone with the password could replace the second factor, defeating it."""
    res = client.post("/admin-api/auth/enrol-totp", {"challenge": "nonsense"},
                      content_type="application/json")
    assert res.status_code == 401
    from adminapi.auth import StaffAuthError, start_enrolment
    with pytest.raises(StaffAuthError) as exc:
        start_enrolment(staffer.username, PASSWORD)
    assert exc.value.code == "already_enrolled"
