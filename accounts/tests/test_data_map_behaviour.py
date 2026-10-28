"""T3 · what the data map makes true: the purge erases the AS2 additions, and the export is
v2 — richer for the subject, and still nobody else's data.
"""
import json
from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken

from accounts.factories import AccountFactory
from accounts.lifecycle import soft_delete_account
from accounts.models import AccountIdentity
from accounts.purge import anonymize_expired_accounts


def _gone(**kw):
    """A deleted account whose grace window has closed."""
    account = AccountFactory(**kw)
    soft_delete_account(account)
    account.deleted_at = timezone.now() - timedelta(days=31)
    account.save(update_fields=["deleted_at"])
    return account


def _hdr(account):
    token = AccessToken()
    token["account_id"] = str(account.account_id)
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def _export(client, account):
    res = client.get("/api/v1/me/export", **_hdr(account))
    assert res.status_code == 200
    return res.json()


# ------------------------------------------------------------------------ erasure (AS2)

@pytest.mark.django_db
def test_the_purge_deletes_linked_sign_ins():
    account = _gone()
    AccountIdentity.objects.create(account=account, provider="google",
                                   provider_user_id="g-1", email=account.email)

    anonymize_expired_accounts()

    # X2: otherwise the same Google login signs straight into the anonymised row.
    assert not AccountIdentity.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_the_purge_deletes_notifications_and_codes():
    from notifications.models import Notification
    from verifications.models import VerificationCode
    account = _gone()
    Notification.objects.create(account=account, type="badge_earned", title="Badge", body="x")
    VerificationCode.objects.create(
        account=account, channel="email", purpose="reset", code_hash="h",
        expires_at=timezone.now() + timedelta(minutes=5))

    anonymize_expired_accounts()

    assert not Notification.objects.filter(account=account).exists()
    assert not VerificationCode.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_the_purge_deletes_pets_and_their_photos_but_not_a_file_a_listing_still_uses():
    from listings.models import AdoptionListing, AdoptionListingPhoto, Pet, PetPhoto
    owner = AccountFactory()
    listing = AdoptionListing.objects.create(posted_by=owner, name="Mochi", species="dog",
                                             city="Marikina")
    AdoptionListingPhoto.objects.create(listing=listing, url="https://bkt/listing/shared.jpg")
    account = _gone()
    pet = Pet.objects.create(owner_account=account, name="Mochi", species="dog",
                             vet_contact="Dr Cruz 0917", emergency_contact="Ana 0918")
    PetPhoto.objects.create(pet=pet, url="https://bkt/listing/shared.jpg")   # copied from the listing
    own = f"https://bkt/listing_photo/{account.pk}/own.jpg"
    PetPhoto.objects.create(pet=pet, url=own)
    # PP5 · a URL that is not this account's own upload is never deleted, whatever it names.
    PetPhoto.objects.create(pet=pet, url="https://bkt/report_photo/someone-else/x.jpg")

    with mock.patch("accounts.purge.delete_object") as delete_object:
        anonymize_expired_accounts()

    assert not Pet.objects.filter(owner_account=account).exists()
    assert not PetPhoto.objects.filter(pet_id=pet.pk).exists()
    deleted = {call.args[0] for call in delete_object.call_args_list}
    assert own in deleted
    assert "https://bkt/report_photo/someone-else/x.jpg" not in deleted
    # The poster is an active account and their listing is public: the file stays.
    assert "https://bkt/listing/shared.jpg" not in deleted
    assert AdoptionListing.objects.filter(pk=listing.pk).exists()


@pytest.mark.django_db
def test_the_purge_scrubs_the_donation_qr_but_keeps_the_row():
    from shelter.models import DonationQr
    account = _gone(account_type="shelter")
    qr = DonationQr.objects.create(account=account, provider="gcash", account_name="Ana Reyes",
                                   qr_image_url="https://bkt/donation_qr/a.png")

    with mock.patch("accounts.purge.delete_object"):
        anonymize_expired_accounts()

    qr.refresh_from_db()
    assert qr.account_name == "" and qr.qr_image_url == ""      # a payee name + payment QR
    assert qr.provider == "gcash"                               # the audit row itself stays


@pytest.mark.django_db
def test_the_welfare_record_survives():
    from django.contrib.gis.geos import Point

    from sagip.models import StrayReport
    account = _gone()
    report = StrayReport.objects.create(species="dog", condition="injured",
                                        geom=Point(121.05, 14.63, srid=4326),
                                        reporter_account=account)

    anonymize_expired_accounts()

    assert StrayReport.objects.filter(pk=report.pk, reporter_account=account).exists()


# ---------------------------------------------------------------------- export (v2)

@pytest.mark.django_db
def test_the_export_is_v2_and_lists_identities_without_the_provider_subject(client):
    account = AccountFactory()
    AccountIdentity.objects.create(account=account, provider="google",
                                   provider_user_id="SECRET-SUB-123", email=account.email)

    body = _export(client, account)

    assert body["export_format"] == "kupkop.export.v2"
    assert body["identities"][0]["provider"] == "google"
    assert "SECRET-SUB-123" not in json.dumps(body)      # the provider's id for you is not yours to dump


@pytest.mark.django_db
def test_the_export_lists_verification_history_without_documents_or_reviewers(client):
    from verifications.models import VerificationDocument, VerificationRequest
    reviewer = AccountFactory(account_type="admin", display_name="Reviewer Rina")
    account = AccountFactory()
    vr = VerificationRequest.objects.create(account=account, type="rescuer", status="approved",
                                            reviewed_by=reviewer, notes="internal note")
    VerificationDocument.objects.create(verification=vr, doc_type="gov_id",
                                        file_url="https://restricted/doc.pdf", status="approved",
                                        review_note="Looks good", reviewed_by=reviewer)

    body = _export(client, account)
    raw = json.dumps(body)

    assert body["verifications"][0]["type"] == "rescuer"
    assert body["verifications"][0]["documents"][0]["doc_type"] == "gov_id"
    assert "https://restricted/doc.pdf" not in raw       # restricted bucket: never a link in a dump
    assert "Reviewer Rina" not in raw and str(reviewer.account_id) not in raw
    assert "internal note" not in raw                    # reviewer-only


@pytest.mark.django_db
def test_the_export_lists_devices_without_the_push_token(client):
    from devices.models import DeviceToken
    account = AccountFactory()
    DeviceToken.objects.create(account=account, fcm_token="FCM-SECRET-TOKEN", platform="ios")

    body = _export(client, account)

    assert body["devices"][0]["platform"] == "ios"
    assert "FCM-SECRET-TOKEN" not in json.dumps(body)


@pytest.mark.django_db
def test_the_export_lists_flags_the_caller_filed_and_no_one_elses(client):
    import uuid

    from moderation.models import ModerationFlag
    account = AccountFactory()
    other = AccountFactory()
    ModerationFlag.objects.create(reporter_account=account, target_type="listing",
                                  target_id=uuid.uuid4(), reason="mine-spam")
    ModerationFlag.objects.create(reporter_account=other, target_type="listing",
                                  target_id=uuid.uuid4(), reason="theirs-abuse")

    raw = json.dumps(_export(client, account))

    assert "mine-spam" in raw
    assert "theirs-abuse" not in raw


@pytest.mark.django_db
def test_a_personal_account_gets_no_shelter_sections(client):
    body = _export(client, AccountFactory())

    for section in ("shelter_profile", "needs", "hosted_shifts"):
        assert section not in body


@pytest.mark.django_db
def test_a_shelter_export_carries_its_org_needs_and_shifts_but_no_one_elses_identity(client):
    from community.models import NeedPledge, ShelterNeed
    from shelter.models import ShelterProfile
    from volunteer.models import VolunteerShift, VolunteerSignup
    shelter = AccountFactory(account_type="shelter")
    ShelterProfile.objects.create(account=shelter, org_name="Happy Paws", org_type="shelter",
                                  contact_person_name="Ate Mia", official_phone="+639171112222")
    need = ShelterNeed.objects.create(shelter_account=shelter, title="Dog food", category="food",
                                      quantity_needed=10)
    donor = AccountFactory(display_name="Donor Dani", email="dani.donor@example.com")
    NeedPledge.objects.create(need=need, pledger_account=donor, quantity=3)
    shift = VolunteerShift.objects.create(shelter_account=shelter, type="feeding",
                                          starts_at=timezone.now() + timedelta(days=2),
                                          ends_at=timezone.now() + timedelta(days=2, hours=2),
                                          capacity=3, title="Morning feed")
    volunteer = AccountFactory(display_name="Volunteer Vic", email="vic.vol@example.com",
                               phone="+639170009999")
    VolunteerSignup.objects.create(shift=shift, volunteer_account=volunteer, status="approved")

    body = _export(client, shelter)
    raw = json.dumps(body)

    assert body["shelter_profile"]["org_name"] == "Happy Paws"
    assert body["shelter_profile"]["contact_person_name"] == "Ate Mia"
    assert body["needs"][0]["title"] == "Dog food"
    assert body["needs"][0]["pledges"] == 1                  # a count, never the pledger
    assert body["hosted_shifts"][0]["title"] == "Morning feed"
    assert body["hosted_shifts"][0]["signups"] == {"approved": 1}
    for someone_else in ("Donor Dani", "dani.donor@example.com", "Volunteer Vic",
                         "vic.vol@example.com", "+639170009999"):
        assert someone_else not in raw
