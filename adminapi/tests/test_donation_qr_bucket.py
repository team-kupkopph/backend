"""Abot-tulong QR plan Run 8 · SEC-12 / SEC-13 / SEC-16 against a mocked S3 (the plan could not run them without a bucket): the whole money-path image
lifecycle through the real boto3 code paths. Presign -> upload bytes -> the URL passes the AT10 validator with the bucket host -> a replacement is
reviewed -> the OLD object is deleted (AT21) and the new one stays; a foreign host is refused."""
import pytest
from django.utils import timezone
from moto import mock_aws

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from adminapi.tests.conftest import full_signin
from shelter.models import DonationQr, QrStatus
from verifications.models import VerificationRequest
from verifications.review import approve_request

BUCKET = "kupkop-media-public-test"
PW = "Corr3ct-horse-battery"


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _present(s3, key):
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except Exception:
        return False


def _upload(client, shelter):
    import requests
    res = client.post("/api/v1/media/presign", {"purpose": "donation_qr", "content_type": "image/jpeg"},
                      content_type="application/json", **_hdr(shelter))
    assert res.status_code == 200
    body = res.json()
    up = requests.post(body["upload_url"], data=body["fields"], files={"file": ("q.jpg", b"\xff\xd8\xff qr bytes", "image/jpeg")})
    assert up.status_code in (200, 201, 204)
    return body["file_url"], body["fields"]["key"]


@pytest.mark.django_db
def test_the_qr_image_lifecycle_against_a_mocked_bucket(client, staffer, settings, django_capture_on_commit_callbacks):
    import boto3
    settings.MEDIA_S3_BUCKET_PUBLIC = BUCKET
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        shelter = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
        shelter.set_password(PW); shelter.save()
        approve_request(VerificationRequest.objects.create(account=shelter, type="shelter_org", status="pending"),
                        AccountFactory(account_type="admin", email="rev-bucket@kupkop.ph"))
        auth = {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}

        # SEC-16 · the URL the app stores is https on the bucket host, and the key carries purpose + owner.
        old_url, old_key = _upload(client, shelter)
        assert old_url == f"https://{BUCKET}/{old_key}" and old_key.startswith(f"donation_qr/{shelter.pk}/")
        first = client.post("/api/v1/shelter/donation-qr", {"provider": "gcash", "account_name": "X", "file_url": old_url}, **_hdr(shelter))
        assert first.status_code == 201
        client.post(f"/admin-api/donation-qrs/{first.json()['donation_qr_id']}/verify", {}, content_type="application/json", **auth)

        # SEC-13 · a URL on any other host (even with the right key) is refused: the console never loads a hostile image.
        evil = client.post("/api/v1/shelter/donation-qr", {"provider": "gcash", "account_name": "X", "current_password": PW,
                                                          "file_url": f"https://evil.invalid/{old_key}"}, **_hdr(shelter))
        assert evil.status_code == 400 and evil.json()["error"]["field"] == "file_url"

        # SEC-12 · replace, review, verify: the OLD object disappears from the bucket, the NEW one stays, and both stay private to the lifecycle.
        new_url, new_key = _upload(client, shelter)
        rep = client.post("/api/v1/shelter/donation-qr", {"provider": "gcash", "account_name": "X", "file_url": new_url, "current_password": PW},
                          **_hdr(shelter))
        assert rep.status_code == 201
        assert _present(s3, old_key) and _present(s3, new_key)          # during review, both exist (the old one is still the live code)
        with django_capture_on_commit_callbacks(execute=True):
            ok = client.post(f"/admin-api/donation-qrs/{rep.json()['donation_qr_id']}/verify", {}, content_type="application/json", **auth)
        assert ok.status_code == 200
        assert not _present(s3, old_key), "the superseded live image must be deleted from the bucket (AT21)"
        assert _present(s3, new_key)
        assert DonationQr.objects.get(account=shelter, provider="gcash", status=QrStatus.LIVE).qr_image_url == new_url

        # Taking it offline deletes the live image too.
        live = DonationQr.objects.get(account=shelter, provider="gcash", status=QrStatus.LIVE)
        with django_capture_on_commit_callbacks(execute=True):
            assert client.post(f"/api/v1/shelter/donation-qr/{live.pk}/offline", {}, content_type="application/json", **_hdr(shelter)).status_code == 200
        assert not _present(s3, new_key)
