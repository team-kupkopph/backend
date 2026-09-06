from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory  # same factory the other volunteer tests use
from volunteer.models import ShiftStatus, SignupStatus, VolunteerShift, VolunteerSignup

URL = "/api/v1/me/signups"


def _auth(account):
    c = APIClient()
    c.force_authenticate(user=account)
    return c


def _shift(shelter, **kw):
    now = timezone.now()
    return VolunteerShift.objects.create(
        shelter_account=shelter, type="walking",
        starts_at=kw.get("starts_at", now + timedelta(days=2)),
        ends_at=kw.get("ends_at", now + timedelta(days=2, hours=2)),
        capacity=kw.get("capacity", 4), status=kw.get("status", ShiftStatus.OPEN))


@pytest.mark.django_db
def test_guest_gets_401():
    assert APIClient().get(URL).status_code == 401


@pytest.mark.django_db
def test_buckets_are_disjoint_and_complete():
    shelter = AccountFactory(account_type="shelter", display_name="Paws")
    vol = AccountFactory(account_type="personal")
    now = timezone.now()
    req = VolunteerSignup.objects.create(shift=_shift(shelter), volunteer_account=vol, status=SignupStatus.REQUESTED)
    up = VolunteerSignup.objects.create(shift=_shift(shelter), volunteer_account=vol, status=SignupStatus.APPROVED)
    past = _shift(shelter, starts_at=now - timedelta(days=2), ends_at=now - timedelta(days=2, hours=-2))
    done = VolunteerSignup.objects.create(shift=past, volunteer_account=vol, status=SignupStatus.COMPLETED)
    body = _auth(vol).get(URL).json()
    assert [i["signup_id"] for i in body["requested"]] == [str(req.pk)]
    assert [i["signup_id"] for i in body["upcoming"]] == [str(up.pk)]
    assert [i["signup_id"] for i in body["history"]] == [str(done.pk)]


@pytest.mark.django_db
def test_past_approved_but_unmarked_falls_to_history():
    shelter = AccountFactory(account_type="shelter"); vol = AccountFactory(account_type="personal")
    now = timezone.now()
    s = _shift(shelter, starts_at=now - timedelta(hours=5), ends_at=now - timedelta(hours=3))
    su = VolunteerSignup.objects.create(shift=s, volunteer_account=vol, status=SignupStatus.APPROVED)
    body = _auth(vol).get(URL).json()
    assert body["upcoming"] == []
    assert [i["signup_id"] for i in body["history"]] == [str(su.pk)]


@pytest.mark.django_db
def test_was_late_true_only_inside_cutoff():
    shelter = AccountFactory(account_type="shelter"); vol = AccountFactory(account_type="personal")
    now = timezone.now()
    s = _shift(shelter, starts_at=now + timedelta(hours=6), ends_at=now + timedelta(hours=8))
    _su = VolunteerSignup.objects.create(shift=s, volunteer_account=vol, status=SignupStatus.CANCELLED, cancelled_at=now)
    item = _auth(vol).get(URL).json()["history"][0]
    assert item["was_late"] is True  # cancelled 6h out, inside the 12h cutoff


@pytest.mark.django_db
def test_only_own_signups_and_reliability_present():
    shelter = AccountFactory(account_type="shelter"); vol = AccountFactory(account_type="personal")
    other = AccountFactory(account_type="personal")
    VolunteerSignup.objects.create(shift=_shift(shelter), volunteer_account=other, status=SignupStatus.REQUESTED)
    body = _auth(vol).get(URL).json()
    assert body["requested"] == [] and body["upcoming"] == [] and body["history"] == []
    assert set(body["reliability"]) == {"shifts_completed", "no_shows", "consecutive_no_shows",
                                        "needs_reapproval", "is_reliable"}


@pytest.mark.django_db
def test_no_other_shelter_identity_leaks():
    # US-SEC5-style leak sweep: only the volunteer's own signups' shelter names may appear.
    shelter = AccountFactory(account_type="shelter", display_name="MyShelter")
    noise = AccountFactory(account_type="shelter", display_name="SecretOtherShelter")
    vol = AccountFactory(account_type="personal")
    VolunteerSignup.objects.create(shift=_shift(shelter), volunteer_account=vol, status=SignupStatus.REQUESTED)
    VolunteerSignup.objects.create(shift=_shift(noise), volunteer_account=AccountFactory(account_type="personal"),
                                   status=SignupStatus.REQUESTED)
    import json
    assert "SecretOtherShelter" not in json.dumps(_auth(vol).get(URL).json())
