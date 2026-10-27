"""T7 · an account has at most ONE primary address (X15), and the cleanup the migration runs.

`PUT /me/location` did `get_or_create(account, is_primary=True)`. With no database rule, two
taps racing past the `get` each created a primary row, and every later call raised
`MultipleObjectsReturned` — a 500 on a screen whose only job is to set a city.
"""
import threading

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.maintenance import backfill_account_settings, demote_duplicate_primary_addresses
from accounts.models import Account, AccountSettings, Address
from accounts.tokens import tokens_for


def _hdr(account):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(account)['access']}"}


@pytest.mark.django_db
def test_the_database_refuses_a_second_primary_address():
    account = AccountFactory()
    Address.objects.create(account=account, is_primary=True, city="Marikina")

    with pytest.raises(IntegrityError), transaction.atomic():
        Address.objects.create(account=account, is_primary=True, city="Quezon City")


@pytest.mark.django_db
def test_any_number_of_non_primary_addresses_is_fine_and_so_is_one_primary_each():
    a, b = AccountFactory(), AccountFactory()
    for account in (a, b):
        Address.objects.create(account=account, is_primary=True, city="Marikina")
        Address.objects.create(account=account, is_primary=False, city="Pasig")
        Address.objects.create(account=account, is_primary=False, city="Taguig")

    assert Address.objects.filter(is_primary=True).count() == 2


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_location_puts_leave_one_primary_row_and_no_500(client):
    account = AccountFactory(email_verified_at=timezone.now())
    headers = _hdr(account)
    statuses, barrier = [], threading.Barrier(2)

    def put(city):
        from django.test import Client
        try:
            barrier.wait(timeout=5)
            res = Client().put("/api/v1/me/location", {"city": city},
                               content_type="application/json", **headers)
            statuses.append(res.status_code)
        finally:
            connection.close()          # threaded tests must release their own connection

    threads = [threading.Thread(target=put, args=(c,)) for c in ("Marikina", "Pasig")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert statuses == [200, 200]
    assert Address.objects.filter(account=account, is_primary=True).count() == 1


@pytest.mark.django_db
def test_putting_a_city_twice_updates_the_same_row(client):
    account = AccountFactory(email_verified_at=timezone.now())

    for city in ("Marikina", "Pasig"):
        assert client.put("/api/v1/me/location", {"city": city},
                          content_type="application/json", **_hdr(account)).status_code == 200

    assert list(Address.objects.filter(account=account).values_list("city", flat=True)) == ["Pasig"]


# ---------------------------------------------------- the migration's two cleanup steps

def _drop_the_rule():
    """Let the test create the duplicates the OLD code could. Postgres DDL is transactional,
    so the pytest-django transaction rolls this back and the rule is back for the next test."""
    with connection.cursor() as cursor:
        cursor.execute("DROP INDEX uq_address_one_primary")


@pytest.mark.django_db
def test_duplicate_primaries_are_demoted_not_deleted():
    account = AccountFactory()
    _drop_the_rule()
    rows = [Address.objects.create(account=account, is_primary=True, city=c)
            for c in ("Marikina", "Pasig", "Taguig")]

    demoted = demote_duplicate_primary_addresses(Address)

    assert demoted == 2
    assert Address.objects.filter(account=account).count() == 3            # nothing lost
    assert Address.objects.filter(account=account, is_primary=True).count() == 1
    # Deterministic: the same one survives a second run.
    survivor = Address.objects.get(account=account, is_primary=True)
    assert demote_duplicate_primary_addresses(Address) == 0
    assert Address.objects.get(account=account, is_primary=True) == survivor
    assert survivor in rows


@pytest.mark.django_db
def test_demoting_touches_only_accounts_that_have_duplicates():
    clean, messy = AccountFactory(), AccountFactory()
    Address.objects.create(account=clean, is_primary=True, city="Marikina")
    _drop_the_rule()
    Address.objects.create(account=messy, is_primary=True, city="Pasig")
    Address.objects.create(account=messy, is_primary=True, city="Taguig")

    assert demote_duplicate_primary_addresses(Address) == 1
    assert Address.objects.get(account=clean).is_primary is True


@pytest.mark.django_db
def test_backfill_creates_a_settings_row_only_where_one_is_missing():
    has_row, missing = AccountFactory(), AccountFactory()
    AccountSettings.objects.filter(account=missing).delete()
    kept = AccountSettings.objects.get(account=has_row)
    kept.marketing_emails = True
    kept.save()

    created = backfill_account_settings(Account, AccountSettings)

    assert created == 1
    assert AccountSettings.objects.filter(account=missing).count() == 1
    assert AccountSettings.objects.get(account=has_row).marketing_emails is True   # untouched
    assert backfill_account_settings(Account, AccountSettings) == 0                # idempotent
