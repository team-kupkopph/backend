"""Abot-tulong QR plan (Run 1) · AT18 (the upload was not throttled) and AT21 (a replaced or withdrawn QR image stayed public at its old URL)."""
from unittest import mock

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from shelter.models import DonationQr, QrStatus
from verifications.models import VerificationRequest
from verifications.review import approve_request

UP = "/api/v1/shelter/donation-qr"


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _shelter():
    s = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    s.set_password("pw-123456-x"); s.save()
    approve_request(VerificationRequest.objects.create(account=s, type="shelter_org", status="pending"),
                    AccountFactory(account_type="admin", email=f"r-{s.pk}@kupkop.ph"))
    return s


def _url(s, name):
    return f"https://example.invalid/donation_qr/{s.pk}/{name}"


def _live(s, name="live.png", provider="gcash"):
    return DonationQr.objects.create(account=s, provider=provider, account_name="X", qr_image_url=_url(s, name), status=QrStatus.LIVE)


@pytest.mark.django_db
def test_AT18_the_upload_is_throttled_per_account(client):
    s = _shelter()
    codes = [client.post(UP, {"provider": "gcash", "account_name": "X", "file_url": _url(s, f"{i}.png")}, **_hdr(s)).status_code for i in range(14)]
    assert codes[:10] == [201] * 10
    assert 429 in codes[10:]


@pytest.mark.django_db
def test_AT18_reading_your_own_qrs_is_not_throttled(client):
    s = _shelter()
    assert all(client.get(UP, **_hdr(s)).status_code == 200 for _ in range(30))


@pytest.mark.django_db
def test_AT21_verifying_a_replacement_deletes_the_old_live_image(client, django_capture_on_commit_callbacks):
    s = _shelter(); live = _live(s)
    pend = DonationQr.objects.create(account=s, provider="gcash", account_name="X", qr_image_url=_url(s, "new.png"),
                                     status=QrStatus.PENDING, replaces=live)
    from shelter import qr_rules
    with mock.patch.object(qr_rules, "delete_object") as gone, django_capture_on_commit_callbacks(execute=True):
        qr_rules.swap_in(pend)
        pend.save()
    gone.assert_called_once_with(live.qr_image_url, "public")
    live.refresh_from_db(); pend.refresh_from_db()
    assert live.status == QrStatus.WITHDRAWN and pend.status == QrStatus.LIVE


@pytest.mark.django_db
def test_AT21_take_offline_and_a_superseded_pending_delete_their_images(client, django_capture_on_commit_callbacks):
    from shelter import qr_rules
    s = _shelter(); live = _live(s)
    with mock.patch.object(qr_rules, "delete_object") as gone, django_capture_on_commit_callbacks(execute=True):
        assert client.post(f"{UP}/{live.pk}/offline", {}, content_type="application/json", **_hdr(s)).status_code == 200
    gone.assert_called_once_with(live.qr_image_url, "public")

    s2 = _shelter(); live2 = _live(s2)
    first = client.post(UP, {"provider": "gcash", "account_name": "X", "file_url": _url(s2, "a.png"), "current_password": "pw-123456-x"}, **_hdr(s2))
    assert first.status_code == 201
    with mock.patch.object(qr_rules, "delete_object") as gone2, django_capture_on_commit_callbacks(execute=True):
        client.post(UP, {"provider": "gcash", "account_name": "X", "file_url": _url(s2, "b.png"), "current_password": "pw-123456-x"}, **_hdr(s2))
    gone2.assert_called_once_with(_url(s2, "a.png"), "public")
    assert live2.qr_image_url  # the LIVE image is untouched by a superseded pending one


@pytest.mark.django_db
def test_AT21_a_refused_image_is_kept_and_a_foreign_url_is_never_deleted(django_capture_on_commit_callbacks):
    from shelter import qr_rules
    s = _shelter()
    legacy = DonationQr.objects.create(account=s, provider="maya", account_name="X", qr_image_url="https://elsewhere.invalid/x.png", status=QrStatus.LIVE)
    with mock.patch.object(qr_rules, "delete_object") as gone, django_capture_on_commit_callbacks(execute=True):
        qr_rules.discard_image(legacy)
    gone.assert_not_called()
