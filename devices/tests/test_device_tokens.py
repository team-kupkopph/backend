import pytest
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from devices.models import DeviceToken

REG = "/api/v1/me/device-tokens"


def _c(a):
    c = APIClient(); c.force_authenticate(user=a); return c


@pytest.mark.django_db
def test_register_creates_token():
    a = AccountFactory()
    r = _c(a).post(REG, {"fcm_token": "tok-1", "platform": "ios"}, format="json")
    assert r.status_code == 201 and "token_id" in r.json()
    t = DeviceToken.objects.get(fcm_token="tok-1")
    assert t.account_id == a.pk and t.platform == "ios"


@pytest.mark.django_db
def test_register_bad_platform_422():
    a = AccountFactory()
    r = _c(a).post(REG, {"fcm_token": "tok-x", "platform": "windows"}, format="json")
    assert r.status_code == 422


@pytest.mark.django_db
def test_reregistering_token_rehomes_it_to_the_new_account():
    # The shared-family-phone fix: the same device token moves to whoever last registered it.
    a = AccountFactory(); b = AccountFactory()
    _c(a).post(REG, {"fcm_token": "shared", "platform": "android"}, format="json")
    _c(b).post(REG, {"fcm_token": "shared", "platform": "android"}, format="json")
    assert DeviceToken.objects.filter(fcm_token="shared").count() == 1     # one row, not a dup/500
    assert DeviceToken.objects.get(fcm_token="shared").account_id == b.pk  # re-homed to b
    assert not DeviceToken.objects.filter(account=a).exists()              # a no longer linked


@pytest.mark.django_db
def test_delete_owner_only():
    a = AccountFactory(); b = AccountFactory()
    t = DeviceToken.objects.create(account=a, fcm_token="tok-2", platform="ios")
    assert _c(b).delete(f"{REG}/{t.token_id}").status_code == 404   # not b's → not found
    assert _c(a).delete(f"{REG}/{t.token_id}").status_code == 204
    assert not DeviceToken.objects.filter(pk=t.token_id).exists()


@pytest.mark.django_db
def test_register_requires_auth():
    assert APIClient().post(REG, {"fcm_token": "z", "platform": "ios"}, format="json").status_code == 401
