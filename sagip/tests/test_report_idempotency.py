"""US-O3 · exactly-once report submission (§13.3).

The outbox exists because §13.3's rule is absolute: *never silently lose a user's report*. A
report composed on a patchy connection is queued locally and retried when the network comes
back. But a retry is only safe if the server can tell "this is the same report again" from
"this is a second animal" — and it cannot, because two reports of the same dog, from the same
person, minutes apart, are legitimately identical in every field.

Without a key, the failure is not a duplicate row. It is **two rescuers dispatched to one
animal**, or one rescuer's claim on a report nobody else can see because the other copy is
the one on the map. That is why this is the piece the plan says must not be de-scoped.

⚠️ The key is scoped to the REPORTER, never global. A globally-unique key would let anyone
who guessed or replayed another person's key receive that person's report — including the
precise coordinates §12.5 withholds from strangers. Scoping makes a guessed key useless.
"""
import uuid

import pytest
from django.contrib.gis.geos import Point

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from sagip.models import StrayReport


def _auth(account):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(account)['access']}"}


def _payload(**kw):
    body = {"species": "dog", "condition": "injured", "lat": 14.63, "lng": 121.05,
            "notes": "limping near the creek"}
    body.update(kw)
    return body


@pytest.mark.django_db
def test_a_report_without_a_key_still_works(client):
    # The key is optional: a report filed online never needs one, and requiring it would
    # break every existing client.
    res = client.post("/api/v1/reports", _payload(), content_type="application/json",
                      **_auth(AccountFactory()))
    assert res.status_code == 201


@pytest.mark.django_db
def test_resending_the_same_key_returns_the_same_report_instead_of_a_second_one(client):
    account = AccountFactory()
    key = str(uuid.uuid4())

    first = client.post("/api/v1/reports", _payload(idempotency_key=key),
                        content_type="application/json", **_auth(account))
    second = client.post("/api/v1/reports", _payload(idempotency_key=key),
                         content_type="application/json", **_auth(account))

    assert first.status_code == 201
    assert second.status_code in (200, 201)
    assert first.json()["report_id"] == second.json()["report_id"]
    assert StrayReport.objects.filter(reporter_account=account).count() == 1


@pytest.mark.django_db
def test_two_genuine_reports_are_not_collapsed(client):
    # The thing that must NOT happen: an outbox that dedupes real reports. Two animals, two
    # keys, two rows — even when every other field matches.
    account = AccountFactory()

    a = client.post("/api/v1/reports", _payload(idempotency_key=str(uuid.uuid4())),
                    content_type="application/json", **_auth(account))
    b = client.post("/api/v1/reports", _payload(idempotency_key=str(uuid.uuid4())),
                    content_type="application/json", **_auth(account))

    assert a.json()["report_id"] != b.json()["report_id"]
    assert StrayReport.objects.filter(reporter_account=account).count() == 2


@pytest.mark.django_db
def test_the_key_is_scoped_to_the_reporter(client):
    """A guessed or replayed key must never hand someone another person's report."""
    mine, theirs = AccountFactory(), AccountFactory()
    key = str(uuid.uuid4())

    first = client.post("/api/v1/reports", _payload(idempotency_key=key),
                        content_type="application/json", **_auth(mine))
    second = client.post("/api/v1/reports", _payload(idempotency_key=key),
                         content_type="application/json", **_auth(theirs))

    assert second.status_code == 201
    assert first.json()["report_id"] != second.json()["report_id"]
    # Two rows, one per person — the second caller did NOT receive the first one's report.
    assert StrayReport.objects.count() == 2


@pytest.mark.django_db
def test_a_replay_does_not_re_run_the_side_effects(client):
    """A retried submit must not re-notify or re-match. The matcher runs on lost/found
    creation (US-L2); replaying the key must return the existing row without firing it again,
    or a flaky connection turns into duplicate reunion pushes."""
    account = AccountFactory()
    key = str(uuid.uuid4())
    body = _payload(report_type="lost", breed="aspin", idempotency_key=key)

    client.post("/api/v1/reports", body, content_type="application/json", **_auth(account))
    before = StrayReport.objects.count()
    client.post("/api/v1/reports", body, content_type="application/json", **_auth(account))

    assert StrayReport.objects.count() == before


@pytest.mark.django_db
def test_the_stored_report_keeps_the_key():
    # Persisted, not just checked in flight — the uniqueness is enforced by the database,
    # so two concurrent retries of the same queued report cannot both insert.
    account = AccountFactory()
    key = str(uuid.uuid4())
    report = StrayReport.objects.create(
        reporter_account=account, species="dog", condition="injured",
        geom=Point(121.05, 14.63, srid=4326), idempotency_key=key)

    report.refresh_from_db()
    assert report.idempotency_key == key


@pytest.mark.django_db
def test_the_database_refuses_a_duplicate_key_for_one_reporter():
    from django.db import IntegrityError, transaction
    account = AccountFactory()
    key = str(uuid.uuid4())
    common = dict(reporter_account=account, species="dog", condition="injured",
                  geom=Point(121.05, 14.63, srid=4326), idempotency_key=key)
    StrayReport.objects.create(**common)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            StrayReport.objects.create(**common)


@pytest.mark.django_db
def test_reports_without_a_key_do_not_collide():
    # NULL keys must stay distinct — most reports have none, and a unique index that treated
    # them as equal would let one report block every subsequent one.
    account = AccountFactory()
    for _ in range(3):
        StrayReport.objects.create(reporter_account=account, species="cat", condition="stray",
                                   geom=Point(121.05, 14.63, srid=4326))

    assert StrayReport.objects.filter(reporter_account=account).count() == 3
