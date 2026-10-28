"""Backlog · settle the pledges stranded by accounts deleted BEFORE backend#110 (one-time data migration).

Since #110 deleting an account settles the other side in the same transaction. Accounts deleted earlier left
a donor's card reading "Pledged" for a shelter that is gone and a shelter planning around goods from a donor
who is gone. This settles them the way the departure settling would have, and nothing else:

  * a leaving DONOR's waiting pledges end as `donor_left`;
  * a leaving SHELTER's open needs resolve the way Mark complete does (`fulfilled` if received >= needed,
    else `closed`) and every pledge still waiting on them ends as `need_closed`; a `fulfilled` need still
    holding a waiting late pledge keeps its status and has those pledges settled.

SILENT (those events are long past, and notifying now would be noise), IDEMPOTENT (a repeat finds nothing),
and it never touches `quantity_received`. Not reversible: which rows were stranded is not recorded.
`report_stranded_pledges` is the read-only preview of exactly this, so after it runs that report is empty.
"""
import importlib

import pytest
from django.apps import apps
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.models import Account
from community.audit import stranded_pledge_report
from community.models import NeedPledge, ShelterNeed
from community.needs import settle_departed_accounts
from notifications.models import Notification


def _delete(account):
    Account.objects.filter(pk=account.pk).update(status="deleted", deleted_at=timezone.now())
    account.refresh_from_db()
    return account


def _need(shelter, *, status="open", needed=10, received=0, title="Dog food"):
    return ShelterNeed.objects.create(shelter_account=shelter, title=title, category="food",
                                      quantity_needed=needed, quantity_received=received, status=status)


def _pledge(need, donor, *, status="pledged"):
    return NeedPledge.objects.create(need=need, pledger_account=donor, quantity=2, status=status)


def _settle():
    return settle_departed_accounts(Account, ShelterNeed, NeedPledge, timezone.now())


def _shelter(**kw):
    return AccountFactory(account_type="shelter", **kw)


# ------------------------------------------------------------------ a leaving donor

@pytest.mark.django_db
def test_a_deleted_donors_waiting_pledge_ends_as_donor_left():
    pledge = _pledge(_need(_shelter()), _delete(AccountFactory()))

    counts = _settle()

    pledge.refresh_from_db()
    assert (pledge.status, pledge.cancel_reason) == ("cancelled", "donor_left")
    assert pledge.decided_at is not None
    assert counts["donor_left"] == 1


@pytest.mark.django_db
def test_facts_and_live_donors_are_left_alone():
    need = _need(_shelter())
    gone = _delete(AccountFactory())
    delivered, cancelled = _pledge(need, gone, status="delivered"), _pledge(need, gone, status="cancelled")
    live = _pledge(need, AccountFactory())

    _settle()

    for p, status in ((delivered, "delivered"), (cancelled, "cancelled"), (live, "pledged")):
        p.refresh_from_db()
        assert p.status == status
    live.refresh_from_db()
    assert live.cancel_reason is None


# ------------------------------------------------------------------ a leaving shelter

@pytest.mark.django_db
def test_an_open_need_of_a_deleted_shelter_closes_and_its_pledges_end_as_need_closed():
    need = _need(_delete(_shelter()), needed=10, received=3)
    pledge = _pledge(need, AccountFactory())

    counts = _settle()

    need.refresh_from_db()
    pledge.refresh_from_db()
    assert need.status == "closed"
    assert (pledge.status, pledge.cancel_reason) == ("cancelled", "need_closed")
    assert counts["needs_settled"] == 1 and counts["need_closed"] == 1


@pytest.mark.django_db
def test_an_open_need_that_was_already_fully_received_resolves_as_fulfilled():
    need = _need(_delete(_shelter()), needed=10, received=10)

    _settle()

    need.refresh_from_db()
    assert need.status == "fulfilled" and need.fulfilled_at is not None


@pytest.mark.django_db
def test_a_fulfilled_need_still_holding_a_waiting_pledge_keeps_its_status():
    stamp = timezone.now() - timezone.timedelta(days=40)
    need = _need(_delete(_shelter()), status="fulfilled", received=10)
    ShelterNeed.objects.filter(pk=need.pk).update(fulfilled_at=stamp)
    pledge = _pledge(need, AccountFactory())

    _settle()

    need.refresh_from_db()
    pledge.refresh_from_db()
    assert need.status == "fulfilled" and need.fulfilled_at == stamp
    assert (pledge.status, pledge.cancel_reason) == ("cancelled", "need_closed")


@pytest.mark.django_db
def test_a_fulfilled_need_with_nothing_waiting_and_a_closed_need_are_untouched():
    shelter = _delete(_shelter())
    done = _need(shelter, status="fulfilled", title="done")
    _pledge(done, AccountFactory(), status="delivered")
    closed = _need(shelter, status="closed", title="closed")

    counts = _settle()

    done.refresh_from_db()
    closed.refresh_from_db()
    assert (done.status, closed.status) == ("fulfilled", "closed")
    assert counts["needs_settled"] == 0


@pytest.mark.django_db
def test_a_live_shelters_needs_are_untouched():
    need = _need(_shelter())
    pledge = _pledge(need, AccountFactory())

    _settle()

    need.refresh_from_db()
    pledge.refresh_from_db()
    assert need.status == "open" and pledge.status == "pledged"


@pytest.mark.django_db
def test_a_pledge_whose_donor_and_shelter_are_both_gone_ends_once_as_need_closed():
    need = _need(_delete(_shelter()))
    pledge = _pledge(need, _delete(AccountFactory()))

    counts = _settle()

    pledge.refresh_from_db()
    assert (pledge.status, pledge.cancel_reason) == ("cancelled", "need_closed")   # the structural fact wins
    assert counts["need_closed"] == 1 and counts["donor_left"] == 0                  # counted exactly once


# ------------------------------------------------------------------ what it promises

@pytest.mark.django_db
def test_it_is_silent_and_never_moves_a_received_quantity():
    need = _need(_delete(_shelter()), needed=10, received=4)
    _pledge(need, AccountFactory())
    _pledge(_need(_shelter(), title="other"), _delete(AccountFactory()))

    _settle()

    assert Notification.objects.count() == 0
    need.refresh_from_db()
    assert need.quantity_received == 4


@pytest.mark.django_db
def test_a_second_run_finds_nothing():
    _pledge(_need(_delete(_shelter())), AccountFactory())
    _pledge(_need(_shelter(), title="other"), _delete(AccountFactory()))
    first = _settle()

    second = _settle()

    assert first["need_closed"] == 1 and first["donor_left"] == 1
    assert second == {"donor_left": 0, "need_closed": 0, "needs_settled": 0}


@pytest.mark.django_db
def test_afterwards_the_read_only_report_has_nothing_left_to_report():
    """The report is the preview of exactly this: same question, so it must come back empty."""
    _pledge(_need(_delete(_shelter())), AccountFactory())
    _pledge(_need(_delete(_shelter()), status="fulfilled", title="held"), AccountFactory())
    _pledge(_need(_shelter(), title="other"), _delete(AccountFactory()))
    before = stranded_pledge_report()

    _settle()
    after = stranded_pledge_report()

    assert before["pledges_to_settle"] == 3 and before["shelter_needs"]["count"] == 2
    assert after["pledges_to_settle"] == 0 and after["shelter_needs"]["count"] == 0
    assert after["scanned"]["deleted_accounts"] == before["scanned"]["deleted_accounts"]   # same denominator


# ------------------------------------------------------------------ the migration itself

@pytest.mark.django_db
def test_the_migration_runs_the_settling_with_the_real_models_and_is_one_way():
    module = importlib.import_module("community.migrations.0009_settle_pledges_of_departed_accounts")
    pledge = _pledge(_need(_shelter()), _delete(AccountFactory()))

    module.settle(apps, None)

    pledge.refresh_from_db()
    assert (pledge.status, pledge.cancel_reason) == ("cancelled", "donor_left")
    from django.db import migrations
    reverse = [op for op in module.Migration.operations if isinstance(op, migrations.RunPython)][0].reverse_code
    assert reverse is migrations.RunPython.noop            # irreversible, and says so
