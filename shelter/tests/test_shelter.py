import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from shelter.models import ShelterProfile


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _shelter(**kw):
    return AccountFactory(account_type="shelter", email_verified_at=timezone.now(), **kw)


# ---------- US-B2 · Set up the organisation ----------

@pytest.mark.django_db
def test_create_profile_writes_profile_and_city_address(client):
    acc = _shelter()
    res = client.post("/api/v1/shelter/profile", {
        "org_name": "PAWS Manila", "org_type": "shelter", "tier": "community_rescue",
        "address": {"line1": "12 Aurora Blvd", "barangay": "Sto. Niño", "city": "Marikina"},
    }, content_type="application/json", **_hdr(acc))
    assert res.status_code == 201
    prof = ShelterProfile.objects.get(account=acc)
    assert prof.tier == "community_rescue" and prof.org_name == "PAWS Manila"
    from accounts.models import Address
    addr = Address.objects.get(account=acc, is_primary=True)
    assert addr.city == "Marikina" and addr.geom is None   # decision 11: no org coordinate unless provided


@pytest.mark.django_db
def test_create_profile_is_forbidden_for_personal_accounts(client):
    acc = AccountFactory(account_type="personal", email_verified_at=timezone.now())
    res = client.post("/api/v1/shelter/profile", {
        "org_name": "X", "org_type": "shelter", "tier": "community_rescue",
        "address": {"city": "Marikina"}}, content_type="application/json", **_hdr(acc))
    assert res.status_code == 403


@pytest.mark.django_db
def test_create_profile_requires_verified_email(client):
    acc = AccountFactory(account_type="shelter", email_verified_at=None)
    res = client.post("/api/v1/shelter/profile", {
        "org_name": "X", "org_type": "shelter", "tier": "community_rescue",
        "address": {"city": "Marikina"}}, content_type="application/json", **_hdr(acc))
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "email_unverified"


@pytest.mark.django_db
def test_second_profile_returns_409(client):
    acc = _shelter()
    body = {"org_name": "PAWS", "org_type": "shelter", "tier": "community_rescue",
            "address": {"city": "Marikina"}}
    client.post("/api/v1/shelter/profile", body, content_type="application/json", **_hdr(acc))
    res = client.post("/api/v1/shelter/profile", body, content_type="application/json", **_hdr(acc))
    assert res.status_code == 409 and res.json()["error"]["code"] == "profile_exists"


@pytest.mark.django_db
def test_registration_number_required_when_type_set(client):
    acc = _shelter()
    res = client.post("/api/v1/shelter/profile", {
        "org_name": "PAWS", "org_type": "shelter", "tier": "registered_ngo",
        "registration_type": "SEC", "address": {"city": "Marikina"}},
        content_type="application/json", **_hdr(acc))
    assert res.status_code == 400


# ---------- US-B3 · contact PATCH ----------

@pytest.mark.django_db
def test_patch_contact_updates_profile(client):
    acc = _shelter()
    ShelterProfile.objects.create(account=acc, org_name="PAWS", org_type="shelter",
                                  tier="community_rescue")
    res = client.patch("/api/v1/shelter/profile", {
        "contact_person_name": "Maria Santos", "contact_person_role": "Volunteer lead",
        "official_phone": "+639171234567", "website_url": "https://paws.ph"},
        content_type="application/json", **_hdr(acc))
    assert res.status_code == 200
    acc.shelter_profile.refresh_from_db()
    assert acc.shelter_profile.contact_person_name == "Maria Santos"


@pytest.mark.django_db
def test_patch_contact_stores_official_phone_as_e164(client):
    # F12 · same normalisation as /me/phone; official_phone is NOT unique (may equal the owner's)
    acc = _shelter()
    ShelterProfile.objects.create(account=acc, org_name="PAWS", org_type="shelter",
                                  tier="community_rescue")
    res = client.patch("/api/v1/shelter/profile", {"official_phone": "0917 123 4567"},
                       content_type="application/json", **_hdr(acc))
    assert res.status_code == 200
    assert res.json()["official_phone"] == "+639171234567"
    acc.shelter_profile.refresh_from_db()
    assert acc.shelter_profile.official_phone == "+639171234567"


@pytest.mark.django_db
def test_patch_contact_rejects_landline_official_phone(client):
    acc = _shelter()
    ShelterProfile.objects.create(account=acc, org_name="PAWS", org_type="shelter",
                                  tier="community_rescue")
    res = client.patch("/api/v1/shelter/profile", {"official_phone": "0281234567"},
                       content_type="application/json", **_hdr(acc))
    assert res.status_code == 400
    err = res.json()["error"]
    assert err["field"] == "official_phone"
    assert err["message"] == "Enter a Philippine mobile number, e.g. 0917 123 4567"
    acc.shelter_profile.refresh_from_db()
    assert acc.shelter_profile.official_phone == ""


@pytest.mark.django_db
def test_patch_contact_blank_official_phone_clears_it(client):
    acc = _shelter()
    ShelterProfile.objects.create(account=acc, org_name="PAWS", org_type="shelter",
                                  tier="community_rescue", official_phone="+639171234567")
    res = client.patch("/api/v1/shelter/profile", {"official_phone": ""},
                       content_type="application/json", **_hdr(acc))
    assert res.status_code == 200
    acc.shelter_profile.refresh_from_db()
    assert acc.shelter_profile.official_phone == ""


# ---------- US-C1 · vet PATCH + PRC format ----------

@pytest.mark.django_db
def test_patch_vet_accepts_valid_prc(client):
    acc = _shelter()
    ShelterProfile.objects.create(account=acc, org_name="PAWS", org_type="shelter",
                                  tier="registered_ngo")
    res = client.patch("/api/v1/shelter/profile",
                       {"vet_name": "Dr. Cruz", "vet_prc_number": "1234567"},
                       content_type="application/json", **_hdr(acc))
    assert res.status_code == 200


@pytest.mark.django_db
def test_patch_vet_rejects_bad_prc_format(client):
    acc = _shelter()
    ShelterProfile.objects.create(account=acc, org_name="PAWS", org_type="shelter",
                                  tier="registered_ngo")
    res = client.patch("/api/v1/shelter/profile",
                       {"vet_name": "Dr. Cruz", "vet_prc_number": "12"},
                       content_type="application/json", **_hdr(acc))
    assert res.status_code == 400


# ---------- US-B5 · dashboard (derived states) ----------

@pytest.mark.django_db
def test_dashboard_incomplete_when_no_request(client):
    acc = _shelter()
    ShelterProfile.objects.create(account=acc, org_name="PAWS", org_type="shelter",
                                  tier="community_rescue")
    res = client.get("/api/v1/shelter/dashboard", **_hdr(acc))
    assert res.status_code == 200
    body = res.json()
    assert body["verification"]["submitted"] is False
    assert body["verification"]["status"] is None
    assert body["gates"] == {"can_publish": False, "donations_enabled": False}


@pytest.mark.django_db
def test_dashboard_pending_when_request_exists(client):
    acc = _shelter()
    ShelterProfile.objects.create(account=acc, org_name="PAWS", org_type="shelter",
                                  tier="community_rescue")
    from verifications.models import VerificationRequest
    VerificationRequest.objects.create(account=acc, type="shelter_org", status="pending")
    res = client.get("/api/v1/shelter/dashboard", **_hdr(acc))
    body = res.json()
    assert body["verification"]["submitted"] is True
    assert body["verification"]["status"] == "pending"
    assert body["gates"]["can_publish"] is False   # pending, not approved


@pytest.mark.django_db
def test_dashboard_counts_adopted_from_own_listings(client):
    from listings.models import AdoptionListing
    acc = _shelter()
    ShelterProfile.objects.create(account=acc, org_name="PAWS", org_type="shelter",
                                  tier="community_rescue")
    AdoptionListing.objects.create(posted_by=acc, species="dog", name="Rex", city="Marikina",
                                   status="adopted")
    AdoptionListing.objects.create(posted_by=acc, species="dog", name="Fido", city="Marikina",
                                   status="available")
    res = client.get("/api/v1/shelter/dashboard", **_hdr(acc))
    assert res.status_code == 200
    assert res.json()["counts"]["adopted"] == 1
