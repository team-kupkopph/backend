"""US-S1/Q1 · shelters, the Decision B gating table, and donation QRs."""
import pytest

from accounts.factories import AccountFactory
from adminapi.tests.conftest import full_signin
from shelter.models import DonationQr, ShelterProfile
from verifications.models import VerificationRequest


@pytest.fixture
def auth(client, staffer):
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}


def make_shelter(name="Alonzo Rescue", verified=False, tier="community_rescue"):
    account = AccountFactory(account_type="shelter", display_name=name,
                             email=f"{name.replace(' ', '').lower()}@ex.com")
    profile = ShelterProfile.objects.create(account=account, org_name=name,
                                            org_type="rescue_group", tier=tier)
    if verified:
        VerificationRequest.objects.create(account=account, type="shelter_org", status="approved")
    return profile


@pytest.mark.django_db
def test_verified_is_derived_not_stored(client, auth):
    """⚠️ There is no `verified` column on shelter_profile, deliberately. It is derived from an
    approved shelter_org verification_request — a rule this project calls load-bearing."""
    unverified = make_shelter("Unverified Org")
    make_shelter("Verified Org", verified=True)

    rows = client.get("/admin-api/shelters", **auth).json()["results"]
    by_name = {r["org_name"]: r["verified"] for r in rows}
    assert by_name == {"Unverified Org": False, "Verified Org": True}

    # And it moves the moment the underlying request is approved — no column to forget.
    VerificationRequest.objects.create(account=unverified.account, type="shelter_org",
                                       status="approved")
    rows = client.get("/admin-api/shelters?verified=true", **auth).json()["results"]
    assert {r["org_name"] for r in rows} == {"Unverified Org", "Verified Org"}


@pytest.mark.django_db
def test_the_gating_table_names_the_one_thing_blocking_each_capability(client, auth):
    """A reviewer must be able to answer 'why can't this shelter receive donations?' without
    reading the spec."""
    profile = make_shelter("Blocked Org")
    caps = client.get(f"/admin-api/shelters/{profile.shelter_profile_id}", **auth).json()["capabilities"]
    assert caps["listings_public"]["enabled"] is False
    assert caps["donations_enabled"]["enabled"] is False
    assert caps["donations_enabled"]["blocked_by"] == "The org is not verified yet."


@pytest.mark.django_db
def test_donations_need_BOTH_keys_and_the_message_follows_the_missing_one(client, auth):
    """⚠️ Two independent gates (Decision B): the org approved AND the QR verified. Showing
    only the QR flag is how a QR gets marked verified while donations stay off."""
    profile = make_shelter("Two Keys", verified=True)
    url = f"/admin-api/shelters/{profile.shelter_profile_id}"

    caps = client.get(url, **auth).json()["capabilities"]
    assert caps["donations_enabled"]["blocked_by"] == "No donation QR yet."

    qr = DonationQr.objects.create(account=profile.account, provider="gcash",
                                   account_name="Alonzo Rescue", qr_image_url="s3://qr.png")
    caps = client.get(url, **auth).json()["capabilities"]
    assert caps["donations_enabled"]["blocked_by"] == "The donation QR is not verified yet."

    qr.verified = True
    qr.save(update_fields=["verified"])
    caps = client.get(url, **auth).json()["capabilities"]
    assert caps["donations_enabled"]["enabled"] is True
    assert caps["donations_enabled"]["blocked_by"] is None


# -- US-Q1 --------------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_qr_row_always_shows_both_gates(client, auth):
    profile = make_shelter("Unverified Org")
    DonationQr.objects.create(account=profile.account, provider="gcash",
                              account_name="X", qr_image_url="s3://qr.png", verified=True)
    row = client.get("/admin-api/donation-qrs", **auth).json()["results"][0]
    # The QR is verified but the ORG is not, so donations are still off — and the row says so
    # rather than leaving the reviewer to wonder why nothing changed.
    assert row["verified"] is True
    assert row["org_verified"] is False
    assert row["donations_live"] is False


@pytest.mark.django_db
def test_verifying_a_qr_flips_it(client, auth):
    profile = make_shelter("Org", verified=True)
    qr = DonationQr.objects.create(account=profile.account, provider="gcash",
                                   account_name="X", qr_image_url="s3://qr.png")
    res = client.post(f"/admin-api/donation-qrs/{qr.donation_qr_id}/verify", {},
                      content_type="application/json", **auth)
    assert res.status_code == 200 and res.json()["donations_live"] is True
    qr.refresh_from_db()
    assert qr.verified is True


@pytest.mark.django_db
def test_unverifying_requires_a_reason(client, auth):
    """Taking a live donation channel down is the action most likely to be questioned later."""
    profile = make_shelter("Org", verified=True)
    qr = DonationQr.objects.create(account=profile.account, provider="gcash", account_name="X",
                                   qr_image_url="s3://qr.png", verified=True)
    res = client.post(f"/admin-api/donation-qrs/{qr.donation_qr_id}/unverify", {},
                      content_type="application/json", **auth)
    assert res.status_code == 422
    qr.refresh_from_db()
    assert qr.verified is True

    ok = client.post(f"/admin-api/donation-qrs/{qr.donation_qr_id}/unverify",
                     {"notes": "Account name does not match the org."},
                     content_type="application/json", **auth)
    assert ok.status_code == 200
    qr.refresh_from_db()
    assert qr.verified is False


@pytest.mark.django_db
def test_verifying_an_already_verified_qr_is_409(client, auth):
    profile = make_shelter("Org", verified=True)
    qr = DonationQr.objects.create(account=profile.account, provider="gcash", account_name="X",
                                   qr_image_url="s3://qr.png", verified=True)
    res = client.post(f"/admin-api/donation-qrs/{qr.donation_qr_id}/verify", {},
                      content_type="application/json", **auth)
    assert res.status_code == 409


@pytest.mark.django_db
def test_shelters_need_a_staff_token(client):
    assert client.get("/admin-api/shelters").status_code in (401, 403)
    assert client.get("/admin-api/donation-qrs").status_code in (401, 403)
