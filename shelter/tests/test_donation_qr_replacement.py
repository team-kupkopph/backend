"""DW2 (Abot-tulong QR plan, owner decision OQ3): replacing a VERIFIED donation QR never takes the live code down.

The replacement is a new `pending` row that points at the live one (`replaces`); the public endpoint keeps serving only the live,
reviewed image; the reviewer's verify swaps them atomically; a refusal leaves the live code untouched; and the shelter can read
its own rows (AT3) and take a QR offline at once. Replacing a live QR needs the account password (owner decision Q3).
"""
import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from shelter.models import DonationQr, QrStatus
from verifications.models import VerificationRequest
from verifications.review import approve_request

PW = "Corr3ct-horse-battery"
UP = "/api/v1/shelter/donation-qr"


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _shelter():
    s = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    s.set_password(PW)
    s.save()
    reviewer = AccountFactory(account_type="admin", email=f"rev-{s.pk}@kupkop.ph")
    approve_request(VerificationRequest.objects.create(account=s, type="shelter_org", status="pending"), reviewer)
    return s


def _live(shelter, url="https://example.invalid/live.png", provider="gcash"):
    return DonationQr.objects.create(account=shelter, provider=provider, account_name="Marikina AWG",
                                     qr_image_url=url, status=QrStatus.LIVE)


def _post(client, shelter, url, **extra):
    # AT10: the server only accepts this account's own donation_qr upload, so tests post that shape (bare names are wrapped).
    if "/donation_qr/" not in url:
        url = f"https://example.invalid/donation_qr/{shelter.pk}/{url.rsplit('/', 1)[-1]}"
    body = {"provider": "gcash", "account_name": "Marikina AWG", "file_url": url, **extra}
    return client.post(UP, body, **_hdr(shelter))


def _public(client, shelter):
    return client.get(f"/api/v1/shelters/{shelter.pk}/donation-qr")


@pytest.mark.django_db
def test_a_replacement_leaves_the_live_qr_public(client):
    s = _shelter(); live = _live(s)
    res = _post(client, s, "https://example.invalid/new.png", current_password=PW)
    assert res.status_code == 201
    assert res.json()["status"] == "pending" and res.json()["verified"] is False
    live.refresh_from_db()
    assert live.status == QrStatus.LIVE and live.qr_image_url.endswith("live.png")
    pub = _public(client, s)
    assert pub.status_code == 200
    assert [q["qr_image_url"] for q in pub.json()["donation_qrs"]] == ["https://example.invalid/live.png"]


@pytest.mark.django_db
def test_the_pending_replacement_is_never_public(client):
    s = _shelter(); _live(s)
    _post(client, s, "https://example.invalid/new.png", current_password=PW)
    assert "new.png" not in _public(client, s).content.decode()


@pytest.mark.django_db
def test_replacing_a_live_qr_needs_the_password(client):
    s = _shelter(); _live(s)
    for extra in ({}, {"current_password": "wrong"}):
        res = _post(client, s, "https://example.invalid/new.png", **extra)
        assert res.status_code == 403
        assert res.json()["error"]["code"] == "reauth_required"
    assert DonationQr.objects.filter(account=s, status=QrStatus.PENDING).count() == 0


@pytest.mark.django_db
def test_a_first_upload_needs_no_password(client):
    s = _shelter()
    assert _post(client, s, "https://example.invalid/first.png").status_code == 201


@pytest.mark.django_db
def test_one_pending_per_provider_a_second_upload_withdraws_the_first(client):
    s = _shelter(); live = _live(s)
    first = _post(client, s, "https://example.invalid/a.png", current_password=PW).json()["donation_qr_id"]
    second = _post(client, s, "https://example.invalid/b.png", current_password=PW).json()["donation_qr_id"]
    assert DonationQr.objects.get(pk=first).status == QrStatus.WITHDRAWN
    pend = DonationQr.objects.get(pk=second)
    assert pend.status == QrStatus.PENDING and pend.replaces_id == live.pk
    assert DonationQr.objects.filter(account=s, provider="gcash", status=QrStatus.PENDING).count() == 1


@pytest.mark.django_db
def test_a_refused_image_cannot_be_resubmitted_but_a_different_one_can(client):
    s = _shelter(); _live(s)
    bad = DonationQr.objects.create(account=s, provider="gcash", account_name="X",
                                    qr_image_url=f"https://example.invalid/donation_qr/{s.pk}/bad.png", status=QrStatus.REFUSED)
    res = _post(client, s, "https://example.invalid/bad.png", current_password=PW)
    assert res.status_code == 400 and res.json()["error"]["code"] == "refused_image"
    assert _post(client, s, "https://example.invalid/other.png", current_password=PW).status_code == 201
    assert bad.pk  # untouched


@pytest.mark.django_db
def test_the_shelter_can_read_its_own_qrs_live_and_pending(client):
    s = _shelter(); live = _live(s)
    _post(client, s, "https://example.invalid/new.png", current_password=PW)
    res = client.get(UP, **_hdr(s))
    assert res.status_code == 200
    rows = {r["status"]: r for r in res.json()["donation_qrs"]}
    assert set(rows) == {"live", "pending"}
    assert rows["pending"]["replaces"] == str(live.pk)
    assert rows["live"]["qr_image_url"].endswith("live.png")


@pytest.mark.django_db
def test_the_own_qrs_endpoint_is_own_account_and_shelter_only(client):
    a, b = _shelter(), _shelter()
    _live(a)
    assert client.get(UP, **_hdr(b)).json()["donation_qrs"] == []
    assert client.get(UP, **_hdr(AccountFactory(account_type="personal"))).status_code == 403
    assert client.get(UP).status_code == 401


@pytest.mark.django_db
def test_take_my_qr_offline_now_withdraws_the_live_code_at_once(client):
    s = _shelter(); live = _live(s)
    res = client.post(f"{UP}/{live.pk}/offline", {}, content_type="application/json", **_hdr(s))
    assert res.status_code == 200
    live.refresh_from_db()
    assert live.status == QrStatus.WITHDRAWN and live.verified is False
    assert _public(client, s).status_code == 404


@pytest.mark.django_db
def test_offline_is_own_rows_only_and_conflicts_when_not_live(client):
    a, b = _shelter(), _shelter()
    live = _live(a)
    assert client.post(f"{UP}/{live.pk}/offline", {}, content_type="application/json", **_hdr(b)).status_code == 404
    gone = DonationQr.objects.create(account=a, provider="maya", account_name="X", qr_image_url="u", status=QrStatus.REFUSED)
    assert client.post(f"{UP}/{gone.pk}/offline", {}, content_type="application/json", **_hdr(a)).status_code == 409


@pytest.mark.django_db
def test_a_shelter_can_withdraw_its_own_pending_replacement(client):
    s = _shelter(); live = _live(s)
    pid = _post(client, s, "https://example.invalid/new.png", current_password=PW).json()["donation_qr_id"]
    assert client.post(f"{UP}/{pid}/offline", {}, content_type="application/json", **_hdr(s)).status_code == 200
    assert DonationQr.objects.get(pk=pid).status == QrStatus.WITHDRAWN
    live.refresh_from_db(); assert live.status == QrStatus.LIVE


@pytest.mark.django_db
def test_verified_is_derived_from_status():
    s = _shelter()
    q = DonationQr.objects.create(account=s, provider="gcash", account_name="X", qr_image_url="u", status=QrStatus.LIVE)
    assert q.verified is True
    q.status = QrStatus.WITHDRAWN; q.save()
    assert q.verified is False
    legacy = DonationQr.objects.create(account=s, provider="maya", account_name="X", qr_image_url="u", verified=True)
    assert legacy.status == QrStatus.LIVE
