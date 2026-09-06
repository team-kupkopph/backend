import pytest
from django.contrib.auth.models import User
from django_otp.plugins.otp_totp.models import TOTPDevice
from django_otp.oath import TOTP

from accounts.models import Account, StaffProfile

PASSWORD = "correct-horse-battery"


def current_code(device: TOTPDevice) -> str:
    """The code the authenticator app would be showing right now."""
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    return format(totp.token(), f"0{device.digits}d")


@pytest.fixture
def staffer(db):
    """A fully set-up reviewer: Django User + admin Account + StaffProfile + TOTP device."""
    user = User.objects.create_user(
        username="rina@kupkopph.com", email="rina@kupkopph.com",
        password=PASSWORD, is_staff=True,
    )
    account = Account.objects.create(
        account_type="admin", email="rina@kupkopph.com", display_name="Rina Alonzo",
    )
    StaffProfile.objects.create(user=user, account=account)
    device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    user.totp_device = device
    user.admin_account = account
    return user


@pytest.fixture
def unbridged_staffer(db):
    """Staff User with a TOTP device but NO StaffProfile — the setup error US-B1 refuses."""
    user = User.objects.create_user(
        username="orphan@kupkopph.com", email="orphan@kupkopph.com",
        password=PASSWORD, is_staff=True,
    )
    user.totp_device = TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    return user


def login(client, email, password=PASSWORD):
    return client.post("/admin-api/auth/login",
                       {"email": email, "password": password},
                       content_type="application/json")


def full_signin(client, user):
    """Both steps. Returns the token payload."""
    res = login(client, user.username)
    assert res.status_code == 200, res.content
    challenge = res.json()["challenge"]
    res = client.post("/admin-api/auth/verify-otp",
                      {"challenge": challenge, "code": current_code(user.totp_device)},
                      content_type="application/json")
    assert res.status_code == 200, res.content
    return res.json()
