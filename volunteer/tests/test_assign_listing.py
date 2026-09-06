from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from listings.models import AdoptionListing
from volunteer.models import ShiftStatus, SignupStatus, VolunteerShift, VolunteerSignup


def _c(a):
    c = APIClient(); c.force_authenticate(user=a); return c


def _walking_signup(shelter):
    now = timezone.now()
    shift = VolunteerShift.objects.create(shelter_account=shelter, type="walking",
        starts_at=now + timedelta(days=2), ends_at=now + timedelta(days=2, hours=2),
        capacity=4, status=ShiftStatus.OPEN)
    vol = AccountFactory(account_type="personal")
    return VolunteerSignup.objects.create(shift=shift, volunteer_account=vol,
        status=SignupStatus.REQUESTED)


def _listing(owner):
    # AdoptionListing.city is required; status defaults to "available" (mirror listings tests).
    return AdoptionListing.objects.create(posted_by=owner, name="Rex", species="dog", city="Manila")


@pytest.mark.django_db
def test_approve_sets_own_listing():
    shelter = AccountFactory(account_type="shelter"); su = _walking_signup(shelter)
    listing = _listing(shelter)
    r = _c(shelter).post(f"/api/v1/shelter/signups/{su.pk}/approve",
                         {"assigned_listing_id": str(listing.listing_id)}, format="json")
    assert r.status_code == 200
    su.refresh_from_db()
    assert str(su.assigned_listing_id) == str(listing.listing_id)


@pytest.mark.django_db
def test_approve_rejects_foreign_listing():
    shelter = AccountFactory(account_type="shelter"); other = AccountFactory(account_type="shelter")
    su = _walking_signup(shelter); foreign = _listing(other)
    r = _c(shelter).post(f"/api/v1/shelter/signups/{su.pk}/approve",
                         {"assigned_listing_id": str(foreign.listing_id)}, format="json")
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_listing"
    su.refresh_from_db(); assert su.status == SignupStatus.REQUESTED    # not approved


@pytest.mark.django_db
def test_approve_without_listing_still_works():
    shelter = AccountFactory(account_type="shelter"); su = _walking_signup(shelter)
    r = _c(shelter).post(f"/api/v1/shelter/signups/{su.pk}/approve", {}, format="json")
    assert r.status_code == 200
    su.refresh_from_db(); assert su.assigned_listing_id is None


@pytest.mark.django_db
def test_approve_rejects_malformed_listing_id():
    shelter = AccountFactory(account_type="shelter"); su = _walking_signup(shelter)
    r = _c(shelter).post(f"/api/v1/shelter/signups/{su.pk}/approve",
                         {"assigned_listing_id": "not-a-uuid"}, format="json")
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_listing"
    su.refresh_from_db(); assert su.status == SignupStatus.REQUESTED    # not approved
