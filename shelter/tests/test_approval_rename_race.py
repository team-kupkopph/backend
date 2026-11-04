"""F1 / NT-1 (shelter home test plan, Run 1) · an approval and a rename must not interleave.

Step 9 made `PATCH /shelter/profile` lock the `shelter_profile` row and check for an approved `shelter_org` inside
that lock. `approve_request` did not take the same lock, so a PATCH that found no approval (the reviewer's
transaction not yet committed) could save a new `org_name` beside an approval committing a moment later: an
approved org whose name nobody reviewed (AS7).

Now the approval takes the profile row lock first, so the two serialise:
- approval first: the PATCH waits, then sees the approval and answers `409 org_name_locked`;
- rename first: the approval waits for it. A rename while a `shelter_org` request is open is recorded in
  `shelter_profile_change` (outcome `changed`), so the reviewer sees it on the shelter's admin page.
"""
import threading
import time

import pytest
from django.db import connection, transaction
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from shelter.models import ShelterProfile, ShelterProfileChange
from verifications.models import VerificationRequest
from verifications.review import approve_request

URL = "/api/v1/shelter/profile"


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _shelter_with_pending_request():
    acc = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    ShelterProfile.objects.create(account=acc, org_name="Happy Paws", org_type="shelter", tier="community_rescue")
    vr = VerificationRequest.objects.create(account=acc, type="shelter_org", status="pending")
    return acc, vr


def _reviewer():
    return AccountFactory(account_type="admin", email="rina@kupkopph.com")


@pytest.mark.django_db(transaction=True)
def test_an_approval_waits_for_a_profile_edit_holding_the_row():
    acc, vr = _shelter_with_pending_request()
    reviewer = _reviewer()
    locked, events = threading.Event(), []

    def edit():
        try:
            with transaction.atomic():
                ShelterProfile.objects.select_for_update().get(account=acc)   # what the PATCH holds
                locked.set()
                time.sleep(0.6)
                events.append("edit committed")
        finally:
            connection.close()

    def approve():
        try:
            locked.wait(timeout=5)
            approve_request(VerificationRequest.objects.get(pk=vr.pk), reviewer)
            events.append("approval committed")
        finally:
            connection.close()

    threads = [threading.Thread(target=edit), threading.Thread(target=approve)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert events == ["edit committed", "approval committed"]


@pytest.mark.django_db
def test_approval_first_then_the_rename_is_refused():
    acc, vr = _shelter_with_pending_request()
    approve_request(vr, _reviewer())
    from django.test import Client
    r = Client().patch(URL, {"org_name": "Someone Else"}, content_type="application/json", **_hdr(acc))
    assert r.status_code == 409 and r.json()["error"]["code"] == "org_name_locked"
    assert ShelterProfile.objects.get(account=acc).org_name == "Happy Paws"


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["pending", "needs_info"])
def test_a_rename_while_a_request_is_open_is_recorded_for_the_reviewer(client, status):
    acc, vr = _shelter_with_pending_request()
    VerificationRequest.objects.filter(pk=vr.pk).update(status=status)
    r = client.patch(URL, {"org_name": "Happier Paws"}, content_type="application/json", **_hdr(acc))
    assert r.status_code == 200, r.content
    row = ShelterProfileChange.objects.get(account=acc)
    assert (row.field, row.old_value, row.new_value, row.outcome) == (
        "org_name", "Happy Paws", "Happier Paws", "changed")


@pytest.mark.django_db
def test_a_rename_with_no_request_open_records_nothing(client):
    # Before anything is submitted the reviewer reads the name at review time: nothing to record (SQ3).
    acc = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    ShelterProfile.objects.create(account=acc, org_name="Happy Paws", org_type="shelter", tier="community_rescue")
    client.patch(URL, {"org_name": "Happier Paws"}, content_type="application/json", **_hdr(acc))
    assert not ShelterProfileChange.objects.filter(account=acc).exists()
