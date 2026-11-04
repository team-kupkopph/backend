"""SH15 (shelter home review, 2026-10-29) · step 12, a suspended counterparty in the inbox.

The probe (adminapi/tests/test_shelters.py, next to the staff fixtures) confirms SH15: suspending an account touches none of its open inquiries or requested
signups, so the shelter's inbox kept offering them as actionable. SQ4 (decided 2026-11-04) · show the row with a
"Suspended" chip and disable the actions, so the shelter is not left wondering where the request went:

- each inbox row carries `counterparty_suspended`;
- the forward actions (screen, reserve, complete an adoption; approve a signup; accept a placement) are refused
  with 409 `counterparty_suspended`. Declining stays open: closing the request is what the shelter should do.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.models import AccountStatus
from accounts.tokens import tokens_for
from listings.models import AdoptionInquiry, AdoptionListing
from listings.tests.adoption_helpers import inquire, listing, person, post, shelter
from volunteer.models import ShiftStatus, SignupStatus, VolunteerShift, VolunteerSignup

INBOX = "/api/v1/shelter/requests"


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _suspend(acc):
    acc.status = AccountStatus.SUSPENDED
    acc.save(update_fields=["status"])


def _signup(the_shelter, volunteer):
    starts = timezone.now() + timedelta(days=2)
    shift = VolunteerShift.objects.create(shelter_account=the_shelter, type="walking", starts_at=starts,
                                          ends_at=starts + timedelta(hours=2), capacity=5, status=ShiftStatus.OPEN)
    return VolunteerSignup.objects.create(shift=shift, volunteer_account=volunteer, status=SignupStatus.REQUESTED)


@pytest.mark.django_db
class TestInboxFlag:
    def test_every_kind_says_whether_its_counterparty_is_suspended(self, client):
        the_shelter = shelter()
        adopter, volunteer, worker = person(), person(), person()
        inquire(adopter, listing(the_shelter))
        _signup(the_shelter, volunteer)
        placed = AdoptionListing.objects.create(posted_by=worker, name="Kalabaw", species="dog", city="Marikina",
                                                adoption_fee="0", status="pending")
        AdoptionInquiry.objects.create(listing=placed, adopter_account=the_shelter, kind="placement")
        before = {r["kind"]: r["counterparty_suspended"]
                  for r in client.get(INBOX, **_hdr(the_shelter)).json()["results"]}
        assert before == {"adoption": False, "volunteer": False, "placement": False}
        for acc in (adopter, volunteer, worker):
            _suspend(acc)
        after = {r["kind"]: r["counterparty_suspended"]
                 for r in client.get(INBOX, **_hdr(the_shelter)).json()["results"]}
        assert after == {"adoption": True, "volunteer": True, "placement": True}


@pytest.mark.django_db
class TestForwardActionsRefused:
    def _refused(self, res):
        assert res.status_code == 409, res.content
        assert res.json()["error"]["code"] == "counterparty_suspended"

    def test_screen(self):
        the_shelter, adopter = shelter(), person()
        inq = inquire(adopter, listing(the_shelter))
        _suspend(adopter)
        self._refused(post(the_shelter, inq, "screen"))

    def test_reserve_and_complete(self):
        the_shelter, adopter = shelter(), person(member=True)
        inq = inquire(adopter, listing(the_shelter))
        assert post(the_shelter, inq, "screen").status_code == 200
        _suspend(adopter)
        self._refused(post(the_shelter, inq, "reserve"))
        adopter.status = AccountStatus.ACTIVE
        adopter.save(update_fields=["status"])
        assert post(the_shelter, inq, "reserve").status_code == 200
        _suspend(adopter)
        self._refused(post(the_shelter, inq, "complete"))

    def test_declining_an_inquiry_stays_open(self):
        the_shelter, adopter = shelter(), person()
        inq = inquire(adopter, listing(the_shelter))
        _suspend(adopter)
        assert post(the_shelter, inq, "reject", {"reason": "other"}).status_code == 200

    def test_approving_a_signup_is_refused_and_declining_is_not(self, client):
        the_shelter, volunteer = shelter(), person()
        su = _signup(the_shelter, volunteer)
        _suspend(volunteer)
        self._refused(client.post(f"/api/v1/shelter/signups/{su.pk}/approve", **_hdr(the_shelter)))
        assert client.post(f"/api/v1/shelter/signups/{su.pk}/decline", **_hdr(the_shelter)).status_code == 200

    def test_accepting_a_placement_from_a_suspended_rescuer_is_refused_and_declining_is_not(self, client):
        the_shelter, worker = shelter(), person()
        placed = AdoptionListing.objects.create(posted_by=worker, name="Kalabaw", species="dog", city="Marikina",
                                                adoption_fee="0", status="pending")
        inq = AdoptionInquiry.objects.create(listing=placed, adopter_account=the_shelter, kind="placement")
        _suspend(worker)
        self._refused(client.post(f"/api/v1/inquiries/{inq.pk}/accept", **_hdr(the_shelter)))
        assert client.post(f"/api/v1/inquiries/{inq.pk}/decline", **_hdr(the_shelter)).status_code == 200
