"""Abot-tulong wishlist refactor · lifecycle (Task 3 schema; Task 4 adds the behaviour tests).

Schema first: the columns the refactor adds, and the CHECKs that keep them honest even if a
writer is bypassed.
"""
import pytest
from django.db import IntegrityError, transaction

from accounts.factories import AccountFactory
from community.models import NeedPledge, PledgeStatus
from community.tests.test_needs import _need, _pledge, _shelter
from shelter.models import ShelterProfile


@pytest.mark.django_db
def test_the_refactor_columns_exist_with_their_checks():
    need = _need()
    assert need.unit == ""
    assert need.fulfilled_at is None

    pledge = _pledge(need)
    assert pledge.quantity_received is None
    assert pledge.decided_at is None
    assert pledge.cancel_reason is None
    assert pledge.note == ""

    # quantity_received is NULL or >= 1
    with pytest.raises(IntegrityError), transaction.atomic():
        NeedPledge.objects.create(need=need, pledger_account=AccountFactory(), quantity=1,
                                  quantity_received=0)
    # cancel_reason is NULL or one of donor / need_closed
    with pytest.raises(IntegrityError), transaction.atomic():
        NeedPledge.objects.create(need=need, pledger_account=AccountFactory(), quantity=1,
                                  status=PledgeStatus.CANCELLED, cancel_reason="x")

    # the valid values save
    ok = NeedPledge.objects.create(need=need, pledger_account=AccountFactory(), quantity=2,
                                   quantity_received=1, cancel_reason="need_closed",
                                   note="left at the gate")
    ok.refresh_from_db()
    assert (ok.quantity_received, ok.cancel_reason, ok.note) == (1, "need_closed", "left at the gate")
    NeedPledge.objects.create(need=need, pledger_account=AccountFactory(), quantity=1,
                              cancel_reason="donor")

    # the composite index is what the aggregates in Task 5 lean on
    names = {i.name for i in NeedPledge._meta.indexes}
    assert {"idx_need_pledge_need", "idx_need_pledge_pledger", "idx_need_pledge_need_status"} <= names


@pytest.mark.django_db
def test_shelter_profile_donation_instructions_defaults_empty():
    sh = _shelter()
    profile = ShelterProfile.objects.create(account=sh, org_name="Paws", org_type="shelter")
    assert profile.donation_instructions == ""
