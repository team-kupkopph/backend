"""US-Q1 · upload/replace a donation QR — draft-first (not gated on org approval), and
any edit resets `verified` to false and re-queues it for review."""
import pytest

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from shelter.models import DonationQr


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _shelter():
    return AccountFactory(account_type="shelter")


@pytest.mark.django_db
def test_creates_a_new_unverified_qr(client):
    # No verification_request exists for this shelter at all — upload is draft-first,
    # gated on org approval only at the public read side (US-Q2), not here.
    shelter = _shelter()
    res = client.post("/api/v1/shelter/donation-qr",
                      {"provider": "gcash", "account_name": "Marikina AWG",
                       "file_url": "https://example.invalid/qr1.png"},
                      **_hdr(shelter))
    assert res.status_code == 201
    body = res.json()
    assert body["verified"] is False
    qr = DonationQr.objects.get(pk=body["donation_qr_id"])
    assert qr.provider == "gcash" and qr.account_name == "Marikina AWG"


@pytest.mark.django_db
def test_a_non_shelter_account_is_refused(client):
    owner = AccountFactory(account_type="personal")
    res = client.post("/api/v1/shelter/donation-qr",
                      {"provider": "gcash", "account_name": "x",
                       "file_url": "https://example.invalid/qr1.png"},
                      **_hdr(owner))
    assert res.status_code == 403


@pytest.mark.django_db
def test_guest_is_refused(client):
    res = client.post("/api/v1/shelter/donation-qr",
                      {"provider": "gcash", "account_name": "x",
                       "file_url": "https://example.invalid/qr1.png"})
    assert res.status_code == 401


@pytest.mark.django_db
def test_reposting_the_same_provider_before_review_replaces_the_pending_row(client):
    # DW2: still ONE pending row per provider (the newer upload withdraws the older one), so the reviewer's queue never holds two for a provider.
    shelter = _shelter()
    first = client.post("/api/v1/shelter/donation-qr",
                        {"provider": "gcash", "account_name": "Marikina AWG",
                         "file_url": "https://example.invalid/qr1.png"}, **_hdr(shelter)).json()["donation_qr_id"]

    res = client.post("/api/v1/shelter/donation-qr",
                      {"provider": "gcash", "account_name": "Marikina AWG (new)",
                       "file_url": "https://example.invalid/qr2.png"}, **_hdr(shelter))

    assert DonationQr.objects.filter(account=shelter, provider="gcash", status="pending").count() == 1
    assert DonationQr.objects.get(pk=first).status == "withdrawn"
    qr = DonationQr.objects.get(pk=res.json()["donation_qr_id"])
    assert qr.qr_image_url == "https://example.invalid/qr2.png"
    assert qr.account_name == "Marikina AWG (new)"


@pytest.mark.django_db
def test_editing_an_already_verified_qr_keeps_it_verified_until_the_replacement_is_reviewed(client):
    # Was `test_editing_an_already_verified_qr_resets_verified_to_false`, which encoded the OLD rule (DW2 / OQ3 replaced it).
    shelter = _shelter()
    shelter.set_password("pw-123456-x"); shelter.save()
    qr = DonationQr.objects.create(account=shelter, provider="gcash", account_name="Marikina AWG",
                                   qr_image_url="https://example.invalid/qr1.png", verified=True)

    res = client.post("/api/v1/shelter/donation-qr",
                      {"provider": "gcash", "account_name": "Marikina AWG",
                       "file_url": "https://example.invalid/qr2-swapped.png", "current_password": "pw-123456-x"},
                      **_hdr(shelter))

    qr.refresh_from_db()
    assert qr.verified is True and qr.status == "live"
    assert res.json()["verified"] is False and res.json()["status"] == "pending"


@pytest.mark.django_db
def test_a_different_provider_is_a_separate_qr(client):
    shelter = _shelter()
    client.post("/api/v1/shelter/donation-qr",
               {"provider": "gcash", "account_name": "Marikina AWG",
                "file_url": "https://example.invalid/gcash.png"}, **_hdr(shelter))
    client.post("/api/v1/shelter/donation-qr",
               {"provider": "maya", "account_name": "Marikina AWG",
                "file_url": "https://example.invalid/maya.png"}, **_hdr(shelter))

    assert DonationQr.objects.filter(account=shelter).count() == 2
