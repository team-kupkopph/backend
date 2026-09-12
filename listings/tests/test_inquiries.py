"""US-A4 — POST /listings/{id}/inquiries, GET /me/inquiries,
POST /inquiries/{id}/stages/{stage_key}."""
import uuid

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from listings.models import AdoptionInquiry, AdoptionListing, AdoptionStage, AdoptionStageHistory
from notifications.models import Notification
from shelter.models import ShelterProfile
from verifications.models import AccountCapability, VerificationRequest


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _verified_member(phone_verified=True):
    acc = AccountFactory(phone_verified_at=timezone.now() if phone_verified else None)
    AccountCapability.objects.create(account=acc, capability="rescuer", status="approved",
                                     granted_at=timezone.now())
    return acc


def _verified_shelter():
    acc = AccountFactory(account_type="shelter", phone_verified_at=timezone.now())
    VerificationRequest.objects.create(account=acc, type="shelter_org", status="approved")
    ShelterProfile.objects.create(account=acc, org_name="Some Shelter", org_type="shelter",
                                  tier="community_rescue")
    return acc


def _listing(poster, **kw):
    defaults = dict(name="Bantay", species="dog", city="Marikina", adoption_fee="300.00")
    defaults.update(kw)
    return AdoptionListing.objects.create(posted_by=poster, **defaults)


def _inquire(client, listing, acc, message=""):
    return client.post(f"/api/v1/listings/{listing.pk}/inquiries", {"message": message},
                       content_type="application/json", **_hdr(acc))


# ── The gate ──────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_a_guest_is_401d(client):
    listing = _listing(AccountFactory())
    res = client.post(f"/api/v1/listings/{listing.pk}/inquiries", {},
                      content_type="application/json")
    assert res.status_code == 401


@pytest.mark.django_db
def test_an_unverified_owner_cannot_inquire(client):
    listing = _listing(AccountFactory())
    plain = AccountFactory(phone_verified_at=timezone.now())
    res = _inquire(client, listing, plain)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "member_badge_required"  # matches the story's own contract


@pytest.mark.django_db
def test_a_verified_shelter_cannot_inquire_only_verified_members_can(client):
    """Deliberately narrower than IsVerifiedRescuer — decision 3, adopting is the
    Pet-Owner path."""
    listing = _listing(AccountFactory())
    shelter = _verified_shelter()
    res = _inquire(client, listing, shelter)
    assert res.status_code == 403


@pytest.mark.django_db
def test_a_verified_member_with_no_verified_phone_is_blocked(client):
    listing = _listing(AccountFactory())
    member = _verified_member(phone_verified=False)
    res = _inquire(client, listing, member)
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "phone_unverified"


@pytest.mark.django_db
def test_inquiring_on_a_missing_listing_is_404(client):
    res = client.post(f"/api/v1/listings/{uuid.uuid4()}/inquiries", {},
                      content_type="application/json", **_hdr(_verified_member()))
    assert res.status_code == 404


# ── The happy path — creates the inquiry + the full stage ladder ────────────────
@pytest.mark.django_db
def test_inquiring_creates_the_inquiry_and_all_six_stages(client):
    poster = AccountFactory()
    listing = _listing(poster)
    member = _verified_member()
    res = _inquire(client, listing, member, message="I'd love to meet Bantay!")
    assert res.status_code == 201
    body = res.json()
    inquiry = AdoptionInquiry.objects.get(pk=body["inquiry_id"])
    assert inquiry.adopter_account_id == member.pk and inquiry.message == "I'd love to meet Bantay!"
    assert body["status"] == "active"

    stages = {s.stage_key: s.state for s in AdoptionStage.objects.filter(inquiry=inquiry)}
    assert len(stages) == 6
    assert stages["inquiry"] == "done"          # submitting IS this stage completing
    assert stages["application"] == "not_started"
    assert stages["finalization"] == "not_started"
    # the inquiry-stage completion is logged like any other transition
    assert AdoptionStageHistory.objects.filter(inquiry=inquiry, stage_key="inquiry").count() == 1


@pytest.mark.django_db
def test_inquiring_notifies_the_poster(client):
    poster = AccountFactory()
    listing = _listing(poster)
    _inquire(client, listing, _verified_member())
    assert Notification.objects.filter(account=poster, type="inquiry_received").exists()


@pytest.mark.django_db
def test_a_second_inquiry_from_the_same_member_on_the_same_listing_is_409(client):
    listing = _listing(AccountFactory())
    member = _verified_member()
    ok = _inquire(client, listing, member)
    assert ok.status_code == 201
    dup = _inquire(client, listing, member)
    assert dup.status_code == 409
    assert AdoptionInquiry.objects.filter(listing=listing, adopter_account=member).count() == 1


@pytest.mark.django_db
def test_the_same_member_can_inquire_on_two_different_listings(client):
    poster = AccountFactory()
    listing1, listing2 = _listing(poster, name="Bantay"), _listing(poster, name="Luna")
    member = _verified_member()
    assert _inquire(client, listing1, member).status_code == 201
    assert _inquire(client, listing2, member).status_code == 201


# ── GET /me/inquiries ─────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_my_inquiries_lists_only_my_own_with_stage_states(client):
    poster = AccountFactory()
    listing = _listing(poster, name="Bantay", species="dog")
    me, other = _verified_member(), _verified_member()
    _inquire(client, listing, me)
    _inquire(client, listing, other)

    body = client.get("/api/v1/me/inquiries", **_hdr(me)).json()
    assert len(body["results"]) == 1
    row = body["results"][0]
    assert row["listing"]["name"] == "Bantay" and row["listing"]["species"] == "dog"
    assert row["status"] == "active"
    stage_states = {s["stage_key"]: s["state"] for s in row["stages"]}
    assert stage_states["inquiry"] == "done"
    assert len(row["stages"]) == 6
    # The ladder shows WHEN a stage moved. All six rows exist from the first second, so a
    # not_started stage's row time is its creation time and is sent as null — only a stage
    # that has moved carries a date.
    by_key = {s["stage_key"]: s for s in row["stages"]}
    assert "T" in by_key["inquiry"]["updated_at"]
    assert by_key["home_check"]["updated_at"] is None
    assert by_key["home_check"]["note"] is None


@pytest.mark.django_db
def test_my_inquiries_requires_auth(client):
    assert client.get("/api/v1/me/inquiries").status_code == 401


# ── POST /inquiries/{id}/stages/{stage_key} ──────────────────────────────────────
@pytest.mark.django_db
def test_the_poster_can_advance_a_stage(client):
    poster = AccountFactory()
    listing = _listing(poster)
    member = _verified_member()
    inquiry_id = _inquire(client, listing, member).json()["inquiry_id"]

    res = client.post(f"/api/v1/inquiries/{inquiry_id}/stages/application",
                      {"state": "done", "note": "Form looks good"},
                      content_type="application/json", **_hdr(poster))
    assert res.status_code == 200
    assert res.json() == {"stage_key": "application", "state": "done"}
    stage = AdoptionStage.objects.get(inquiry_id=inquiry_id, stage_key="application")
    assert stage.state == "done" and stage.note == "Form looks good"
    assert AdoptionStageHistory.objects.filter(inquiry_id=inquiry_id, stage_key="application").count() == 1


@pytest.mark.django_db
def test_only_the_poster_can_advance_a_stage(client):
    poster = AccountFactory()
    listing = _listing(poster)
    member = _verified_member()
    inquiry_id = _inquire(client, listing, member).json()["inquiry_id"]

    res = client.post(f"/api/v1/inquiries/{inquiry_id}/stages/application", {"state": "done"},
                      content_type="application/json", **_hdr(member))  # the adopter, not the poster
    assert res.status_code == 403


@pytest.mark.django_db
def test_advancing_a_stage_notifies_the_adopter(client):
    poster = AccountFactory()
    listing = _listing(poster)
    member = _verified_member()
    inquiry_id = _inquire(client, listing, member).json()["inquiry_id"]

    client.post(f"/api/v1/inquiries/{inquiry_id}/stages/home_check", {"state": "in_progress"},
               content_type="application/json", **_hdr(poster))
    assert Notification.objects.filter(account=member, type="stage_advanced").exists()


@pytest.mark.django_db
def test_advancing_an_unknown_inquiry_is_404(client):
    res = client.post(f"/api/v1/inquiries/{uuid.uuid4()}/stages/application", {"state": "done"},
                      content_type="application/json", **_hdr(AccountFactory()))
    assert res.status_code == 404


@pytest.mark.django_db
def test_advancing_an_unknown_stage_key_is_404(client):
    poster = AccountFactory()
    listing = _listing(poster)
    inquiry_id = _inquire(client, listing, _verified_member()).json()["inquiry_id"]
    res = client.post(f"/api/v1/inquiries/{inquiry_id}/stages/not_a_real_stage", {"state": "done"},
                      content_type="application/json", **_hdr(poster))
    assert res.status_code == 404


@pytest.mark.django_db
def test_an_invalid_state_value_is_rejected(client):
    poster = AccountFactory()
    listing = _listing(poster)
    inquiry_id = _inquire(client, listing, _verified_member()).json()["inquiry_id"]
    res = client.post(f"/api/v1/inquiries/{inquiry_id}/stages/application",
                      {"state": "passed"},  # not a real StageState value
                      content_type="application/json", **_hdr(poster))
    assert res.status_code == 400
