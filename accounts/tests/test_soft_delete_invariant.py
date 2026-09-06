"""US-C1 · the M5 invariant: `deleted_at` and `status='deleted'` move together.

The root DDL has carried this CHECK since the data-analyst review, but neither the two
columns nor the constraint existed in any model or migration — so nothing enforced it and
nothing noticed, because check-docs compared TABLE names only. These tests are the
behavioural half of closing that (the column-level guard in `dev/check-docs.py` is the
mechanical half).

Why it matters beyond tidiness: Sprint 7's §12.7 purge sweep selects on
`deleted_at + grace`. A row with `status='deleted'` and a NULL `deleted_at` would never be
purged (a deletion that silently never completes), and a `deleted_at` on an ACTIVE account
would make the sweep anonymise a live user. The database refuses both.
"""
import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.models import Account, AccountStatus


@pytest.mark.django_db
def test_marking_deleted_without_a_timestamp_is_refused():
    account = AccountFactory()
    account.status = AccountStatus.DELETED          # no deleted_at — no grace window
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            account.save()


@pytest.mark.django_db
def test_a_deletion_timestamp_on_an_active_account_is_refused():
    account = AccountFactory()
    account.deleted_at = timezone.now()             # would make the purge eat a live user
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            account.save()


@pytest.mark.django_db
def test_the_two_facts_together_are_accepted():
    account = AccountFactory()
    account.status = AccountStatus.DELETED
    account.deleted_at = timezone.now()
    account.save()

    account.refresh_from_db()
    assert account.status == AccountStatus.DELETED
    assert account.deleted_at is not None


@pytest.mark.django_db
def test_an_ordinary_account_has_neither():
    account = AccountFactory()
    assert account.status == AccountStatus.ACTIVE
    assert account.deleted_at is None
    assert account.last_active_at is None           # never written as a login side effect


@pytest.mark.django_db
def test_last_active_at_is_writable_without_touching_the_deletion_invariant():
    # Retention/inactive-account policy input (§12.6) — an ordinary nullable timestamp
    # that the CHECK must not accidentally constrain.
    account = AccountFactory()
    account.last_active_at = timezone.now()
    account.save()

    assert Account.objects.filter(pk=account.pk,
                                  last_active_at__isnull=False).exists()
