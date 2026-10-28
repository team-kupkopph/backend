"""Backlog · a read-only audit of pledges stranded by accounts deleted BEFORE backend#110.

Since #110 deleting an account settles the other side in the same transaction: a leaving donor's
`pledged` pledges end as `donor_left`, and a leaving shelter's open needs close through the same writer
Mark complete uses. Accounts deleted before that kept their half-finished commitments: a donor's card
still reads "Pledged" for a shelter that is gone, and a shelter plans around goods from a donor who is gone.

Whether any such rows exist in production is unknown, and fixing them is an irreversible data change, so
this command ONLY REPORTS. It mirrors what the settling does (so it finds exactly what the settling would
have changed), prints its totals even when the answer is zero (a zero with no denominator proves nothing),
and never prints a name or an email: ids only.
"""
import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.models import Account
from community.models import NeedPledge, ShelterNeed
from notifications.models import Notification


def _delete(account):
    """Deleted the way an account was deleted BEFORE #110: the status moves, nothing is settled."""
    Account.objects.filter(pk=account.pk).update(status="deleted", deleted_at=timezone.now())
    account.refresh_from_db()
    return account


def _shelter(**kw):
    return AccountFactory(account_type="shelter", **kw)


def _need(shelter, *, status="open", title="Dog food"):
    return ShelterNeed.objects.create(shelter_account=shelter, title=title, category="food",
                                      quantity_needed=10, quantity_received=0, status=status)


def _pledge(need, donor, *, status="pledged"):
    return NeedPledge.objects.create(need=need, pledger_account=donor, quantity=2, status=status)


def _report(*args):
    out = StringIO()
    call_command("report_stranded_pledges", "--json", *args, stdout=out)
    return json.loads(out.getvalue())


def _text(*args):
    out = StringIO()
    call_command("report_stranded_pledges", *args, stdout=out)
    return out.getvalue()


# ------------------------------------------------------------------ a leaving donor

@pytest.mark.django_db
def test_a_waiting_pledge_from_a_deleted_donor_is_stranded():
    donor = _delete(AccountFactory())
    pledge = _pledge(_need(_shelter()), donor)

    report = _report()

    assert report["donor_pledges"]["count"] == 1
    assert report["donor_pledges"]["rows"][0]["pledge_id"] == str(pledge.pk)
    assert report["pledges_to_settle"] == 1


@pytest.mark.django_db
def test_only_waiting_pledges_from_deleted_donors_count():
    gone = _delete(AccountFactory())
    need = _need(_shelter())
    _pledge(need, gone, status="delivered")           # a recorded fact
    _pledge(need, gone, status="cancelled")
    _pledge(need, AccountFactory())                   # an active donor

    assert _report()["donor_pledges"]["count"] == 0


@pytest.mark.django_db
def test_an_anonymised_account_is_still_a_deleted_one():
    donor = _delete(AccountFactory())
    Account.objects.filter(pk=donor.pk).update(anonymized_at=timezone.now())
    _pledge(_need(_shelter()), donor)

    assert _report()["donor_pledges"]["count"] == 1


# ------------------------------------------------------------------ a leaving shelter

@pytest.mark.django_db
def test_an_open_need_of_a_deleted_shelter_is_stranded():
    shelter = _delete(_shelter())
    need = _need(shelter)

    report = _report()

    assert report["shelter_needs"]["count"] == 1
    assert report["shelter_needs"]["rows"][0]["need_id"] == str(need.pk)


@pytest.mark.django_db
def test_a_fulfilled_need_counts_only_while_it_still_holds_a_waiting_pledge():
    shelter = _delete(_shelter())
    holding = _need(shelter, status="fulfilled", title="holding")
    empty = _need(shelter, status="fulfilled", title="empty")
    _pledge(holding, AccountFactory())
    _pledge(empty, AccountFactory(), status="delivered")
    _need(shelter, status="closed", title="closed")

    report = _report()

    assert [r["need_id"] for r in report["shelter_needs"]["rows"]] == [str(holding.pk)]


@pytest.mark.django_db
def test_the_waiting_pledges_on_a_deleted_shelters_needs_are_counted_too():
    shelter = _delete(_shelter())
    need = _need(shelter)
    pledge = _pledge(need, AccountFactory())
    _pledge(need, AccountFactory(), status="delivered")

    report = _report()

    assert report["shelter_need_pledges"]["count"] == 1
    assert report["shelter_need_pledges"]["rows"][0]["pledge_id"] == str(pledge.pk)


@pytest.mark.django_db
def test_a_pledge_in_both_groups_is_counted_once_in_the_total():
    shelter, donor = _delete(_shelter()), _delete(AccountFactory())
    _pledge(_need(shelter), donor)

    report = _report()

    assert report["donor_pledges"]["count"] == 1 and report["shelter_need_pledges"]["count"] == 1
    assert report["pledges_to_settle"] == 1


@pytest.mark.django_db
def test_a_live_shelters_needs_are_left_alone():
    _pledge(_need(_shelter()), AccountFactory())

    report = _report()

    assert report["shelter_needs"]["count"] == 0 and report["pledges_to_settle"] == 0


# ------------------------------------------------------------------ what the output promises

@pytest.mark.django_db
def test_it_never_writes_anything():
    donor = _delete(AccountFactory())
    _pledge(_need(_delete(_shelter())), donor)
    before = (NeedPledge.objects.filter(status="pledged").count(),
              ShelterNeed.objects.filter(status="open").count(), Notification.objects.count())

    with CaptureQueriesContext(connection) as queries:
        _report()
        _text()

    assert (NeedPledge.objects.filter(status="pledged").count(),
            ShelterNeed.objects.filter(status="open").count(), Notification.objects.count()) == before
    writes = [q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
    assert writes == []


@pytest.mark.django_db
def test_it_prints_what_it_scanned_even_when_there_is_nothing_to_settle():
    """A bare zero cannot be told from a query that matched nothing by mistake."""
    _pledge(_need(_shelter()), AccountFactory())

    report = _report()
    text = _text()

    assert report["pledges_to_settle"] == 0
    assert report["scanned"] == {"deleted_accounts": 0, "waiting_pledges": 1, "open_or_fulfilled_needs": 1}
    assert "scanned" in text.lower() and "nothing to settle" in text.lower()


@pytest.mark.django_db
def test_it_prints_ids_only_never_a_name_or_an_email():
    donor = _delete(AccountFactory(email="secret.donor@example.com", display_name="Secret Donor"))
    shelter = _delete(_shelter(email="secret.shelter@example.com", display_name="Secret Shelter"))
    _pledge(_need(shelter, title="Secret need title"), donor)

    for output in (_text(), json.dumps(_report())):
        for secret in ("secret.donor", "Secret Donor", "secret.shelter", "Secret Shelter", "Secret need"):
            assert secret not in output


@pytest.mark.django_db
def test_the_limit_caps_the_listed_rows_not_the_counts():
    donor = _delete(AccountFactory())
    need = _need(_shelter())
    for _ in range(5):
        _pledge(need, donor)

    report = _report("--limit", "2")

    assert report["donor_pledges"]["count"] == 5
    assert len(report["donor_pledges"]["rows"]) == 2


@pytest.mark.django_db
def test_the_text_says_what_to_do_next_and_that_it_changed_nothing():
    _pledge(_need(_shelter()), _delete(AccountFactory()))

    text = _text()

    assert "1" in text and "changed nothing" in text.lower()
