"""US-Q2 · the public donate surface — renders only verified QRs of approved orgs, both
keys always required."""
import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from shelter.models import DonationQr
from verifications.models import VerificationRequest
from verifications.review import approve_request


def _approved_shelter():
    reviewer = AccountFactory(account_type="admin", email="rev-q2@kupkop.ph")
    shelter = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    vr = VerificationRequest.objects.create(account=shelter, type="shelter_org", status="pending")
    approve_request(vr, reviewer)
    return shelter


def _qr(account, verified, provider="gcash"):
    return DonationQr.objects.create(account=account, provider=provider,
                                     account_name="Marikina AWG",
                                     qr_image_url="https://example.invalid/qr.png",
                                     verified=verified)


def _url(account):
    return f"/api/v1/shelters/{account.pk}/donation-qr"


@pytest.mark.django_db
def test_returns_verified_qrs_of_an_approved_org(client):
    shelter = _approved_shelter()
    _qr(shelter, verified=True)
    res = client.get(_url(shelter))
    assert res.status_code == 200
    qrs = res.json()["donation_qrs"]
    assert len(qrs) == 1 and qrs[0]["provider"] == "gcash"


@pytest.mark.django_db
def test_unapproved_org_is_404_even_with_a_verified_qr(client):
    shelter = AccountFactory(account_type="shelter")
    VerificationRequest.objects.create(account=shelter, type="shelter_org", status="pending")
    _qr(shelter, verified=True)
    res = client.get(_url(shelter))
    assert res.status_code == 404


@pytest.mark.django_db
def test_approved_org_with_no_verified_qr_is_404(client):
    shelter = _approved_shelter()
    _qr(shelter, verified=False)
    res = client.get(_url(shelter))
    assert res.status_code == 404


@pytest.mark.django_db
def test_approved_org_with_no_qr_at_all_is_404(client):
    shelter = _approved_shelter()
    res = client.get(_url(shelter))
    assert res.status_code == 404


@pytest.mark.django_db
def test_an_unverified_qr_never_appears_alongside_a_verified_one(client):
    shelter = _approved_shelter()
    _qr(shelter, verified=True, provider="gcash")
    _qr(shelter, verified=False, provider="maya")
    res = client.get(_url(shelter))
    qrs = res.json()["donation_qrs"]
    assert [q["provider"] for q in qrs] == ["gcash"]


@pytest.mark.django_db
def test_route_is_public_no_auth_needed(client):
    shelter = _approved_shelter()
    _qr(shelter, verified=True)
    res = client.get(_url(shelter))  # no Authorization header at all
    assert res.status_code == 200


# Abot-tulong QR plan Run 1/2 · AT9 + AT25. The gate read the org approval and the QR, never the account:
# a suspended or deleted shelter's payment QR stayed public (a donor still saw it on a device).
@pytest.mark.django_db
@pytest.mark.parametrize("status", ["suspended", "deleted"])
def test_a_suspended_or_deleted_shelters_qr_is_not_public(client, status):
    shelter = _approved_shelter()
    _qr(shelter, verified=True)
    assert client.get(_url(shelter)).status_code == 200
    if status == "deleted":
        from accounts.lifecycle import soft_delete_account
        soft_delete_account(shelter)
    else:
        shelter.status = "suspended"
        shelter.save(update_fields=["status"])
    res = client.get(_url(shelter))
    assert res.status_code == 404
    assert "donation_qrs" not in res.json()


@pytest.mark.django_db
def test_reinstating_a_suspended_shelter_restores_its_qr(client):
    shelter = _approved_shelter()
    _qr(shelter, verified=True)
    shelter.status = "suspended"; shelter.save(update_fields=["status"])
    assert client.get(_url(shelter)).status_code == 404
    shelter.status = "active"; shelter.save(update_fields=["status"])
    assert client.get(_url(shelter)).status_code == 200
