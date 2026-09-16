"""US-... B-be2 — GET /shelter/requests, the shelter's one merged inbox: open adoption
inquiries on its own listings, volunteer signups on its own shifts, and placements
addressed to it (an AdoptionInquiry where it is the adopter and every stage is SKIPPED —
the placement bypass, see listings/tests/test_inquiries.py)."""
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from listings.models import (
    ADOPTION_STAGE_ORDER,
    AdoptionInquiry,
    AdoptionListing,
    AdoptionStage,
    InquiryStatus,
    StageState,
)
from volunteer.models import ShiftStatus, SignupStatus, VolunteerShift, VolunteerSignup

URL = "/api/v1/shelter/requests"


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _shelter(**kw):
    return AccountFactory(account_type="shelter", email_verified_at=timezone.now(), **kw)


def _listing(poster, **kw):
    defaults = dict(name="Bantay", species="dog", city="Marikina", adoption_fee="0")
    defaults.update(kw)
    return AdoptionListing.objects.create(posted_by=poster, **defaults)


def _shift(shelter, **kw):
    now = timezone.now()
    defaults = dict(type="walking", starts_at=now + timedelta(days=2),
                    ends_at=now + timedelta(days=2, hours=2), capacity=2,
                    status=ShiftStatus.OPEN)
    defaults.update(kw)
    return VolunteerShift.objects.create(shelter_account=shelter, **defaults)


def _skip_all_stages(inquiry):
    for stage_key in ADOPTION_STAGE_ORDER:
        AdoptionStage.objects.create(inquiry=inquiry, stage_key=stage_key, state=StageState.SKIPPED)


class _Fixture:
    """One shelter with one open adoption inquiry on its own listing, one volunteer
    signup on its own shift, and one placement (an inquiry where IT is the adopter,
    all stages skipped)."""

    def __init__(self):
        self.shelter = _shelter()
        self.adopter = AccountFactory(account_type="personal")
        self.volunteer = AccountFactory(account_type="personal")
        self.other_poster = AccountFactory(account_type="personal")

        self.listing = _listing(self.shelter, name="Bantay")
        self.inquiry = AdoptionInquiry.objects.create(
            listing=self.listing, adopter_account=self.adopter, status=InquiryStatus.ACTIVE)
        # A real (non-placement) inquiry still gets its stage ladder; only the first
        # stage is DONE (per listings' own "inquiry starts DONE" convention) — the
        # point is simply that NOT every stage is SKIPPED.
        AdoptionStage.objects.create(inquiry=self.inquiry, stage_key=ADOPTION_STAGE_ORDER[0],
                                     state=StageState.DONE)

        self.shift = _shift(self.shelter)
        self.signup = VolunteerSignup.objects.create(
            shift=self.shift, volunteer_account=self.volunteer, status=SignupStatus.REQUESTED)

        self.placed_listing = _listing(self.other_poster, name="Kalabaw")
        self.placement = AdoptionInquiry.objects.create(
            listing=self.placed_listing, adopter_account=self.shelter, status=InquiryStatus.ACTIVE)
        _skip_all_stages(self.placement)


@pytest.mark.django_db
def test_no_filter_returns_all_three_newest_first(client):
    f = _Fixture()
    res = client.get(URL, **_hdr(f.shelter))
    assert res.status_code == 200
    body = res.json()
    assert len(body["results"]) == 3
    kinds = {r["kind"] for r in body["results"]}
    assert kinds == {"adoption", "volunteer", "placement"}
    created_ats = [r["created_at"] for r in body["results"]]
    assert created_ats == sorted(created_ats, reverse=True)


@pytest.mark.django_db
def test_kind_filter_returns_only_that_kind(client):
    f = _Fixture()
    for kind, expected_id in (("adoption", str(f.inquiry.pk)),
                              ("volunteer", str(f.signup.pk)),
                              ("placement", str(f.placement.pk))):
        res = client.get(URL, {"kind": kind}, **_hdr(f.shelter))
        assert res.status_code == 200
        body = res.json()
        assert len(body["results"]) == 1
        assert body["results"][0]["kind"] == kind
        assert body["results"][0]["id"] == expected_id


@pytest.mark.django_db
def test_status_open_after_declining_the_inquiry_leaves_two(client):
    f = _Fixture()
    f.inquiry.status = InquiryStatus.DECLINED
    f.inquiry.decided_at = timezone.now()
    f.inquiry.save(update_fields=["status", "decided_at"])
    res = client.get(URL, {"status": "open"}, **_hdr(f.shelter))
    assert res.status_code == 200
    body = res.json()
    assert len(body["results"]) == 2
    kinds = {r["kind"] for r in body["results"]}
    assert kinds == {"volunteer", "placement"}


@pytest.mark.django_db
def test_another_shelter_sees_nothing(client):
    _Fixture()
    other_shelter = _shelter()
    res = client.get(URL, **_hdr(other_shelter))
    assert res.status_code == 200
    assert res.json()["results"] == []


@pytest.mark.django_db
def test_owner_account_is_403d(client):
    _Fixture()
    owner = AccountFactory(account_type="personal", email_verified_at=timezone.now())
    res = client.get(URL, **_hdr(owner))
    assert res.status_code == 403
