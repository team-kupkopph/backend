"""F3 / NT-2 (shelter home test plan, Run 1) · a storage error while discarding a replaced image.

`_discard_logo` and `discard_image` deleted the old object on commit with no `try`. The change had already
committed, so an S3 error turned a saved PATCH (or a QR decision) into a 500: the app showed a failure for a
change that worked. Now the delete logs the object KEY (never the URL, never an account id) on `kupkop.storage`
and carries on: the orphaned object is left for cleanup, the same posture as the purge's `_delete_file`.
"""
import logging
import uuid

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from common.storage import delete_object_or_log
from shelter.models import ShelterProfile


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


@pytest.fixture
def s3_down(monkeypatch):
    def boom(url, visibility):
        raise ConnectionError("s3 unreachable")

    # Each caller passes its own module's `delete_object` (the name the existing tests patch), so patch all three.
    for where in ("common.storage", "shelter.views", "shelter.qr_rules"):
        monkeypatch.setattr(f"{where}.delete_object", boom)


def test_the_helper_logs_the_key_and_does_not_raise(s3_down, caplog):
    url = "https://bkt.example/shelter_logo/acc-1/obj-9?X-Amz-Signature=secret"
    with caplog.at_level(logging.WARNING, logger="kupkop.storage"):
        delete_object_or_log(url, "public", why="replaced logo")
    [rec] = [r for r in caplog.records if r.name == "kupkop.storage"]
    msg = rec.getMessage()
    assert "shelter_logo/acc-1/obj-9" in msg and "replaced logo" in msg
    assert "Signature" not in msg and "https://" not in msg


@pytest.mark.django_db
def test_a_replaced_logo_whose_delete_fails_still_saves_and_answers_200(
        client, s3_down, caplog, django_capture_on_commit_callbacks):
    acc = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    old = f"https://bkt.example/shelter_logo/{acc.pk}/{uuid.uuid4()}"
    new = f"https://bkt.example/shelter_logo/{acc.pk}/{uuid.uuid4()}"
    ShelterProfile.objects.create(account=acc, org_name="Paws", org_type="shelter", logo_url=old)
    with caplog.at_level(logging.WARNING, logger="kupkop.storage"), \
            django_capture_on_commit_callbacks(execute=True):
        r = client.patch("/api/v1/shelter/profile", {"logo_file_url": new}, content_type="application/json",
                         **_hdr(acc))
    assert r.status_code == 200, r.content
    assert ShelterProfile.objects.get(account=acc).logo_url == new
    assert any(r.name == "kupkop.storage" and "shelter_logo/" in r.getMessage() for r in caplog.records)
    assert not any(str(acc.pk) in r.getMessage() and "https://" in r.getMessage() for r in caplog.records)


@pytest.mark.django_db
def test_a_withdrawn_qr_whose_delete_fails_does_not_raise(s3_down, settings, django_capture_on_commit_callbacks):
    from shelter.models import DonationQr, QrStatus
    from shelter.qr_rules import discard_image
    acc = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    qr = DonationQr.objects.create(account=acc, provider="gcash", account_name="Paws",
                                   qr_image_url=f"https://bkt.example/donation_qr/{acc.pk}/{uuid.uuid4()}",
                                   status=QrStatus.WITHDRAWN)
    with django_capture_on_commit_callbacks(execute=True):
        discard_image(qr)          # must not raise from the on-commit delete
