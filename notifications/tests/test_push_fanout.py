from unittest.mock import patch

import pytest

from accounts.factories import AccountFactory
from devices.models import DeviceToken
from notifications import push as pushmod
from notifications.service import notify


@pytest.mark.django_db
def test_fanout_sends_for_pushable_type(django_capture_on_commit_callbacks):
    a = AccountFactory()
    DeviceToken.objects.create(account=a, fcm_token="t1", platform="ios")
    with patch("notifications.service.send_push", return_value=pushmod.PushResult(ok=True)) as sp:
        with django_capture_on_commit_callbacks(execute=True):
            notify(a, "shift_confirmed", title="x", body="y", data={"shift_id": "s", "signup_id": "g"})
    assert sp.called   # one token → one send, after commit


@pytest.mark.django_db
def test_no_push_when_push_enabled_false(django_capture_on_commit_callbacks):
    a = AccountFactory(); a.settings.push_enabled = False; a.settings.save()
    DeviceToken.objects.create(account=a, fcm_token="t1", platform="ios")
    with patch("notifications.service.send_push") as sp:
        with django_capture_on_commit_callbacks(execute=True):
            notify(a, "shift_confirmed", title="x", body="y", data={})
    assert not sp.called


@pytest.mark.django_db
def test_unregistered_prunes_the_token(django_capture_on_commit_callbacks):
    a = AccountFactory()
    DeviceToken.objects.create(account=a, fcm_token="dead", platform="ios")
    with patch("notifications.service.send_push", return_value=pushmod.PushResult(ok=False, unregistered=True)):
        with django_capture_on_commit_callbacks(execute=True):
            notify(a, "shift_confirmed", title="x", body="y", data={})
    assert not DeviceToken.objects.filter(fcm_token="dead").exists()


@pytest.mark.django_db
def test_push_failure_never_breaks_notify(django_capture_on_commit_callbacks):
    a = AccountFactory()
    DeviceToken.objects.create(account=a, fcm_token="t1", platform="ios")
    with patch("notifications.service.send_push", side_effect=RuntimeError("boom")):
        with django_capture_on_commit_callbacks(execute=True):
            n = notify(a, "shift_confirmed", title="x", body="y", data={})   # must not raise
    assert n.notification_id is not None   # the in-app row still exists
