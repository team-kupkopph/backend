"""SH16 (shelter home review, 2026-10-29) · shelter telemetry, account-free.

Nothing in `shelter/` called `emit()`, so the shelter funnel (signup → profile → approved → first listing) and
time to first response on a request were not measurable. Step 8 adds three events, all coarse and with no
account id or field value (SQ2: no per-shelter metric yet):

- `shelter_profile_created {tier, org_type}`
- `shelter_profile_updated {fields: [...]}`, names only, and only the fields that actually changed
- `shelter_request_decided {kind, outcome, age_bucket}` when a SHELTER answers a request
"""
import json
import logging
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from listings.models import AdoptionInquiry, AdoptionListing, InquiryStatus
from shelter.models import ShelterProfile
from shelter.telemetry import age_bucket
from volunteer.models import ShiftStatus, SignupStatus, VolunteerShift, VolunteerSignup
from volunteer.sweeps import decline_lapsed_requests


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _events(caplog, name):
    out = []
    for r in caplog.records:
        if r.name != "kupkop.analytics":
            continue
        p = json.loads(r.getMessage())
        if p["event"] == name:
            out.append(p)
    return out


@pytest.fixture
def analytics(caplog):
    caplog.set_level(logging.INFO, logger="kupkop.analytics")
    return caplog


def _shelter(**kw):
    return AccountFactory(account_type="shelter", email_verified_at=timezone.now(), **kw)


def _profile(acc, **kw):
    defaults = dict(org_name="Paws Inc.", org_type="shelter", tier="registered_ngo")
    defaults.update(kw)
    return ShelterProfile.objects.create(account=acc, **defaults)


class TestAgeBucket:
    @pytest.mark.parametrize("age,bucket", [
        (timedelta(minutes=0), "lt_1h"), (timedelta(minutes=59), "lt_1h"),
        (timedelta(hours=1), "1h_24h"), (timedelta(hours=23, minutes=59), "1h_24h"),
        (timedelta(hours=24), "1d_3d"), (timedelta(days=3), "3d_7d"),
        (timedelta(days=7), "7d_plus"), (timedelta(days=400), "7d_plus"),
    ])
    def test_boundaries(self, age, bucket):
        now = timezone.now()
        assert age_bucket(now - age, now=now) == bucket

    def test_a_clock_skewed_future_timestamp_is_lt_1h_not_an_error(self):
        now = timezone.now()
        assert age_bucket(now + timedelta(minutes=5), now=now) == "lt_1h"


@pytest.mark.django_db
class TestProfileEvents:
    def test_created_carries_tier_and_org_type_and_nothing_identifying(self, client, analytics):
        acc = _shelter()
        r = client.post("/api/v1/shelter/profile", {
            "org_name": "Paws of Marikina", "org_type": "rescue", "tier": "community_rescue",
            "address": {"city": "Marikina City"},
        }, content_type="application/json", **_hdr(acc))
        assert r.status_code == 201, r.content
        [ev] = _events(analytics, "shelter_profile_created")
        assert {k: v for k, v in ev.items() if k != "ts"} == {
            "event": "shelter_profile_created", "tier": "community_rescue", "org_type": "rescue"}

    def test_updated_lists_only_the_fields_that_changed_by_name(self, client, analytics):
        acc = _shelter()
        _profile(acc, official_phone="09170000000")
        r = client.patch("/api/v1/shelter/profile", {
            "org_name": "Paws Inc.",                    # re-sent unchanged: the edit form sends everything
            "official_phone": "09171112222", "description": "We rescue.",
        }, content_type="application/json", **_hdr(acc))
        assert r.status_code == 200, r.content
        [ev] = _events(analytics, "shelter_profile_updated")
        assert ev["fields"] == ["description", "official_phone"]
        assert "09171112222" not in json.dumps(ev)

    def test_a_patch_that_changes_nothing_emits_nothing(self, client, analytics):
        acc = _shelter()
        _profile(acc)
        client.patch("/api/v1/shelter/profile", {"org_name": "Paws Inc."},
                     content_type="application/json", **_hdr(acc))
        assert _events(analytics, "shelter_profile_updated") == []

    def test_a_refused_patch_emits_nothing(self, client, analytics):
        acc = _shelter()
        _profile(acc)
        r = client.patch("/api/v1/shelter/profile", {"official_email": "not-an-email"},
                         content_type="application/json", **_hdr(acc))
        assert r.status_code == 400
        assert _events(analytics, "shelter_profile_updated") == []


def _shift(shelter, ends_in=timedelta(days=2)):
    ends = timezone.now() + ends_in
    return VolunteerShift.objects.create(shelter_account=shelter, type="walking", starts_at=ends - timedelta(hours=2),
                                         ends_at=ends, capacity=5, status=ShiftStatus.OPEN)


def _signup(shift, age=timedelta(hours=2)):
    su = VolunteerSignup.objects.create(shift=shift, volunteer_account=AccountFactory(account_type="personal"),
                                        status=SignupStatus.REQUESTED)
    VolunteerSignup.objects.filter(pk=su.pk).update(created_at=timezone.now() - age)
    return su


@pytest.mark.django_db
class TestRequestDecided:
    @pytest.mark.parametrize("action,outcome", [("approve", "approved"), ("decline", "declined")])
    def test_a_signup_decision(self, client, analytics, django_capture_on_commit_callbacks, action, outcome):
        shelter = _shelter()
        su = _signup(_shift(shelter), age=timedelta(hours=2))
        with django_capture_on_commit_callbacks(execute=True):
            r = client.post(f"/api/v1/shelter/signups/{su.pk}/{action}", **_hdr(shelter))
        assert r.status_code == 200, r.content
        [ev] = _events(analytics, "shelter_request_decided")
        assert {k: v for k, v in ev.items() if k != "ts"} == {
            "event": "shelter_request_decided", "kind": "volunteer", "outcome": outcome, "age_bucket": "1h_24h"}

    def test_a_refused_decision_emits_nothing(self, client, analytics, django_capture_on_commit_callbacks):
        shelter = _shelter()
        su = _signup(_shift(shelter))
        VolunteerSignup.objects.filter(pk=su.pk).update(status=SignupStatus.DECLINED)
        with django_capture_on_commit_callbacks(execute=True):
            r = client.post(f"/api/v1/shelter/signups/{su.pk}/decline", **_hdr(shelter))
        assert r.status_code == 409
        assert _events(analytics, "shelter_request_decided") == []

    def test_the_lapsed_sweep_counts_as_no_answer(self, analytics, django_capture_on_commit_callbacks):
        shelter = _shelter()
        _signup(_shift(shelter, ends_in=-timedelta(hours=1)), age=timedelta(days=4))
        with django_capture_on_commit_callbacks(execute=True):
            decline_lapsed_requests()
        [ev] = _events(analytics, "shelter_request_decided")
        assert (ev["kind"], ev["outcome"], ev["age_bucket"]) == ("volunteer", "lapsed", "3d_7d")

    def test_a_shelter_rejecting_an_adoption_inquiry(self, client, analytics, django_capture_on_commit_callbacks):
        shelter = _shelter()
        listing = AdoptionListing.objects.create(posted_by=shelter, name="Bantay", species="dog", city="Marikina",
                                                 adoption_fee="0", status="available")
        inq = AdoptionInquiry.objects.create(listing=listing, adopter_account=AccountFactory(account_type="personal"))
        AdoptionInquiry.objects.filter(pk=inq.pk).update(created_at=timezone.now() - timedelta(days=8))
        with django_capture_on_commit_callbacks(execute=True):
            r = client.post(f"/api/v1/inquiries/{inq.pk}/reject", {"reason": "not_a_fit"},
                            content_type="application/json", **_hdr(shelter))
        assert r.status_code == 200, r.content
        [ev] = _events(analytics, "shelter_request_decided")
        assert (ev["kind"], ev["outcome"], ev["age_bucket"]) == ("adoption", "declined", "7d_plus")

    def test_a_person_rejecting_their_own_inquiry_is_not_a_shelter_event(
            self, client, analytics, django_capture_on_commit_callbacks):
        poster = AccountFactory(account_type="personal", email_verified_at=timezone.now())
        listing = AdoptionListing.objects.create(posted_by=poster, name="Bantay", species="dog", city="Marikina",
                                                 adoption_fee="0", status="available")
        inq = AdoptionInquiry.objects.create(listing=listing, adopter_account=AccountFactory(account_type="personal"))
        with django_capture_on_commit_callbacks(execute=True):
            r = client.post(f"/api/v1/inquiries/{inq.pk}/reject", {"reason": "not_a_fit"},
                            content_type="application/json", **_hdr(poster))
        assert r.status_code == 200, r.content
        assert _events(analytics, "shelter_request_decided") == []

    @pytest.mark.parametrize("action,outcome", [("decline", "declined"), ("accept", "accepted")])
    def test_a_shelter_answering_a_placement(self, client, analytics, django_capture_on_commit_callbacks,
                                            action, outcome):
        shelter = _shelter()
        worker = AccountFactory(account_type="personal")
        listing = AdoptionListing.objects.create(posted_by=worker, name="Kalabaw", species="dog", city="Marikina",
                                                 adoption_fee="0", status="pending")
        inq = AdoptionInquiry.objects.create(listing=listing, adopter_account=shelter, kind="placement",
                                             status=InquiryStatus.ACTIVE)
        with django_capture_on_commit_callbacks(execute=True):
            r = client.post(f"/api/v1/inquiries/{inq.pk}/{action}", **_hdr(shelter))
        assert r.status_code == 200, r.content
        [ev] = _events(analytics, "shelter_request_decided")
        assert (ev["kind"], ev["outcome"], ev["age_bucket"]) == ("placement", outcome, "lt_1h")
