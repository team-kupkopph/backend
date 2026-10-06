"""Shared builders for the adoption poster-loop tests (Tasks 3–7 of
docs/superpowers/plans/2026-10-05-adoption-poster-loop.md)."""
import itertools

from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from accounts.models import Address
from listings.models import AdoptionInquiry, AdoptionListing
from notifications.models import Notification
from shelter.models import ShelterProfile
from verifications.models import AccountCapability, VerificationRequest

_phones = itertools.count(1)


def c(account):
    client = APIClient(); client.force_authenticate(user=account); return client


def _phone():
    return f"+639170{next(_phones):06d}"


def person(member=False, city="Marikina", phone=True):
    """A pet owner with a verified phone (unless phone=False), optionally a Verified Member."""
    acc = AccountFactory(phone=_phone() if phone else None,
                         phone_verified_at=timezone.now() if phone else None)
    Address.objects.create(account=acc, city=city, is_primary=True)
    if member:
        AccountCapability.objects.create(account=acc, capability="rescuer", status="approved",
                                         granted_at=timezone.now())
    return acc


def shelter(official_phone="+63281234567"):
    acc = AccountFactory(account_type="shelter", phone=_phone(), phone_verified_at=timezone.now())
    VerificationRequest.objects.create(account=acc, type="shelter_org", status="approved")
    ShelterProfile.objects.create(account=acc, org_name="Paws Marikina", org_type="shelter",
                                  tier="community_rescue", contact_person_name="Ana Cruz",
                                  official_phone=official_phone)
    return acc


def listing(poster, verify=True, **kw):
    """A listing. Its poster is made public (a Verified Member) unless verify=False, because AD16
    hides an unverified poster's listing from everyone else (Task 2b)."""
    if verify and poster.account_type != "shelter":
        AccountCapability.objects.get_or_create(
            account=poster, capability="rescuer",
            defaults={"status": "approved", "granted_at": timezone.now()})
    defaults = dict(name="Bantay", species="dog", city="Marikina", adoption_fee="300.00")
    defaults.update(kw)
    return AdoptionListing.objects.create(posted_by=poster, **defaults)


def inquire(adopter, the_listing, message=""):
    res = c(adopter).post(f"/api/v1/listings/{the_listing.pk}/inquiries", {"message": message},
                          format="json")
    assert res.status_code == 201, res.content
    return AdoptionInquiry.objects.get(pk=res.json()["inquiry_id"])


def post(who, inquiry, action, body=None):
    return c(who).post(f"/api/v1/inquiries/{inquiry.pk}/{action}", body or {}, format="json")


def types(account):
    return list(Notification.objects.filter(account=account).order_by("created_at")
                .values_list("type", flat=True))
