"""US-A4 — POST /listings/{id}/inquiries, GET /me/inquiries,
POST /inquiries/{id}/stages/{stage_key}."""
import uuid

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.models import Account, AccountStatus
from accounts.tokens import tokens_for
from listings.models import AdoptionInquiry, AdoptionListing, AdoptionStage, AdoptionStageHistory
from notifications.models import Notification
from shelter.models import ShelterProfile
from listings.visibility import account_is_verified_rescuer
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


def _listing(poster, verify=True, **kw):
    # AD16 · only a public poster's listing can be inquired on. Make the poster a Verified Member
    # unless the test is about an unverified one (verify=False).
    if verify and not account_is_verified_rescuer(poster):
        AccountCapability.objects.get_or_create(
            account=poster, capability="rescuer",
            defaults={"status": "approved", "granted_at": timezone.now()})
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
def test_aq2_an_owner_without_the_badge_can_inquire_with_a_verified_phone(client):
    """AQ2 (2026-10-05) · asking needs a verified phone only. The badge moved to Reserve."""
    listing = _listing(AccountFactory())
    plain = AccountFactory(phone_verified_at=timezone.now())
    assert _inquire(client, listing, plain).status_code == 201


@pytest.mark.django_db
def test_a_verified_shelter_cannot_inquire(client):
    """Decision 3 stands: adopting is the pet-owner path."""
    res = _inquire(client, _listing(AccountFactory()), _verified_shelter())
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "shelter_cannot_adopt"


@pytest.mark.django_db
def test_an_owner_without_a_verified_phone_is_blocked(client):
    res = _inquire(client, _listing(AccountFactory()), AccountFactory())
    assert res.status_code == 403 and res.json()["error"]["code"] == "phone_unverified"


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
    # `application` rather than `home_check`: on an individual's listing AQ5 starts home_check
    # SKIPPED (with a date and a note), so it is no longer a not-started stage.
    assert by_key["application"]["updated_at"] is None
    assert by_key["application"]["note"] is None


@pytest.mark.django_db
def test_my_inquiries_requires_auth(client):
    assert client.get("/api/v1/me/inquiries").status_code == 401


# ── GET /inquiries/{id} ───────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_an_adopter_reads_one_inquiry_by_id_and_nobody_else_can(client):
    """C25 · the Place request screen used to scan page 1 of /me/inquiries; the detail is the
    same object as one list row, and only the adopter may read it in the adopter tier (a stranger
    gets 404, not 403, so an inquiry's existence isn't revealed). The listing's own poster now
    reads the poster tier instead (AD1, test_poster_reads.py)."""
    poster = AccountFactory()
    listing = _listing(poster)
    me = _verified_member()
    assert _inquire(client, listing, me).status_code == 201
    inq = AdoptionInquiry.objects.get(listing=listing, adopter_account=me)

    row = client.get(f"/api/v1/inquiries/{inq.pk}", **_hdr(me))
    assert row.status_code == 200
    listed = client.get("/api/v1/me/inquiries", **_hdr(me)).json()["results"][0]
    assert row.json() == listed
    res = client.get(f"/api/v1/inquiries/{inq.pk}", **_hdr(AccountFactory()))
    assert res.status_code == 404 and res.json()["error"]["code"] == "not_found"
    assert client.get(f"/api/v1/inquiries/{uuid.uuid4()}", **_hdr(me)).status_code == 404
    assert client.get(f"/api/v1/inquiries/{inq.pk}").status_code == 401



@pytest.mark.django_db
def test_aq5_an_individuals_listing_starts_with_home_check_and_vet_clearance_skipped(client):
    listing = _listing(AccountFactory())                      # a person, not a shelter
    inquiry_id = _inquire(client, listing, _verified_member()).json()["inquiry_id"]
    stages = {s.stage_key: s for s in AdoptionStage.objects.filter(inquiry_id=inquiry_id)}
    assert stages["home_check"].state == "skipped" and stages["vet_clearance"].state == "skipped"
    assert stages["home_check"].note == "Not needed when adopting from an individual."
    assert stages["application"].state == "not_started"
    hist = AdoptionStageHistory.objects.get(inquiry_id=inquiry_id, stage_key="home_check")
    assert hist.changed_by_account is None                    # the system, not a person


@pytest.mark.django_db
def test_aq5_a_shelters_listing_keeps_all_six_steps(client):
    listing = _listing(_verified_shelter())
    inquiry_id = _inquire(client, listing, _verified_member()).json()["inquiry_id"]
    states = dict(AdoptionStage.objects.filter(inquiry_id=inquiry_id).values_list("stage_key", "state"))
    assert states == {"inquiry": "done", "application": "not_started", "home_check": "not_started",
                      "interview": "not_started", "vet_clearance": "not_started",
                      "finalization": "not_started"}


@pytest.mark.django_db
@pytest.mark.parametrize("make_poster,is_shelter", [(_verified_shelter, True),
                                                    (_verified_member, False)])
def test_the_posters_push_says_whether_a_shelter_posted(client, make_poster, is_shelter):
    """Mobile's interim routing: a shelter opens Requests, an individual opens the listing."""
    poster = make_poster()
    _inquire(client, _listing(poster), _verified_member())
    n = Notification.objects.get(account=poster, type="inquiry_received")
    assert n.data["poster_is_shelter"] is is_shelter


@pytest.mark.django_db
@pytest.mark.parametrize("member", [True, False])
def test_the_inquiry_answer_says_whether_the_adopter_holds_the_badge(client, member):
    adopter = _verified_member() if member else AccountFactory(phone_verified_at=timezone.now())
    body = _inquire(client, _listing(AccountFactory()), adopter).json()
    assert body["verified_member"] is member

# ── POST /inquiries/{id}/stages/{stage_key} ──────────────────────────────────────
@pytest.mark.django_db
def test_the_poster_can_advance_a_stage(client):
    poster = AccountFactory()
    listing = _listing(poster)
    member = _verified_member()
    inquiry_id = _inquire(client, listing, member).json()["inquiry_id"]
    AdoptionInquiry.objects.filter(pk=inquiry_id).update(accepted_at=timezone.now())   # AD3

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
    AdoptionInquiry.objects.filter(pk=inquiry_id).update(accepted_at=timezone.now())   # AD3

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
    AdoptionInquiry.objects.filter(pk=inquiry_id).update(accepted_at=timezone.now())   # AD3
    res = client.post(f"/api/v1/inquiries/{inquiry_id}/stages/application",
                      {"state": "passed"},  # not a real StageState value
                      content_type="application/json", **_hdr(poster))
    assert res.status_code == 400


@pytest.mark.django_db
def test_a_non_numeric_page_falls_back_to_the_first_page(client):
    me = _verified_member()
    _inquire(client, _listing(AccountFactory(), name="Bantay"), me)
    res = client.get("/api/v1/me/inquiries?page=abc", **_hdr(me))
    assert res.status_code == 200
    assert [r["listing"]["name"] for r in res.json()["results"]] == ["Bantay"]


# ── A listing that is no longer available takes no new inquiries ────────────────
@pytest.mark.django_db
@pytest.mark.parametrize("status", ["withdrawn", "pending", "adopted"])
def test_inquiring_on_a_listing_that_is_no_longer_available_is_409(client, status):
    """Final review #1 · a take-back (D15) leaves a WITHDRAWN listing that a stale feed card or a
    deep link can still reach; PENDING is a placement offered to one recipient, ADOPTED has a home.
    None of them may collect a fresh ACTIVE inquiry (which would also notify the poster)."""
    poster = AccountFactory()
    listing = _listing(poster, status=status)
    res = _inquire(client, listing, _verified_member())
    assert res.status_code == 409, res.content
    err = res.json()["error"]
    assert err["code"] == "listing_unavailable"
    assert err["message"] == "This animal is no longer available for adoption."
    assert not AdoptionInquiry.objects.filter(listing=listing).exists()
    assert not Notification.objects.filter(account=poster, type="inquiry_received").exists()


@pytest.mark.django_db
def test_an_inquiry_that_raced_a_take_back_is_refused_not_stranded(client):
    """The status is re-read under the row lock: a listing the poster took back after the view's
    first read still refuses (simulated by changing the row between the first read and the lock)."""
    from unittest import mock

    poster = AccountFactory()
    listing = _listing(poster)
    member = _verified_member()
    real = AdoptionInquiry.objects.filter

    def flip_then_filter(*a, **kw):
        # The view's already_inquired check is the last thing before the locked re-read.
        AdoptionListing.objects.filter(pk=listing.pk).update(status="withdrawn")
        return real(*a, **kw)

    with mock.patch.object(AdoptionInquiry.objects, "filter", side_effect=flip_then_filter):
        res = _inquire(client, listing, member)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "listing_unavailable"
    assert not AdoptionInquiry.objects.filter(listing=listing).exists()


# ── AD13 / AD16 ──────────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_ad13_a_poster_cannot_inquire_on_their_own_listing(client):
    poster = _verified_member()
    res = _inquire(client, _listing(poster), poster)
    assert res.status_code == 422 and res.json()["error"]["code"] == "own_listing"
    assert not AdoptionInquiry.objects.exists()


@pytest.mark.django_db
def test_ad16_an_unverified_posters_listing_is_404_by_link_except_to_the_poster(client):
    poster = AccountFactory(phone_verified_at=timezone.now())       # no badge: not public
    listing = _listing(poster, verify=False)
    assert client.get(f"/api/v1/listings/{listing.pk}").status_code == 404           # guest
    assert client.get(f"/api/v1/listings/{listing.pk}", **_hdr(_verified_member())).status_code == 404
    assert client.get(f"/api/v1/listings/{listing.pk}", **_hdr(poster)).status_code == 200
    res = _inquire(client, listing, _verified_member())
    assert res.status_code == 404 and not AdoptionInquiry.objects.exists()


@pytest.mark.django_db
def test_ad16_a_deleted_posters_listing_is_404_by_link(client):
    poster = _verified_member()
    listing = _listing(poster)
    Account.objects.filter(pk=poster.pk).update(status=AccountStatus.DELETED, deleted_at=timezone.now())
    assert client.get(f"/api/v1/listings/{listing.pk}").status_code == 404
    assert _inquire(client, listing, _verified_member()).status_code == 404
