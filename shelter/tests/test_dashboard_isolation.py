"""SH12b (shelter home review, 2026-10-29) · step 13, one failing block of Home is that block, not all of Home.

`/shelter/dashboard` fans in across six apps. A failure in the rescue map's query (the heaviest) or the wishlist
summary used to 500 the whole Home. Each is now isolated: it answers `null` for its block, logged with the
traceback, inside its own savepoint so the rest of the response still reads the database. Mobile already treats
`rescue` and `wishlist` as optional.
"""
import logging

import pytest
from django.db import connection
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for

DASH = "/api/v1/shelter/dashboard"


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


@pytest.fixture
def shelter():
    return AccountFactory(account_type="shelter", email_verified_at=timezone.now())


@pytest.mark.django_db
@pytest.mark.parametrize("block,helper", [("rescue", "_rescue_summary"), ("wishlist", "_wishlist_summary")])
def test_a_failing_block_answers_null_and_the_rest_of_home_loads(client, shelter, monkeypatch, caplog, block, helper):
    def boom(_shelter):
        raise RuntimeError("map search down")

    monkeypatch.setattr(f"shelter.views.{helper}", boom)
    with caplog.at_level(logging.ERROR, logger="shelter.views"):
        r = client.get(DASH, **_hdr(shelter))
    assert r.status_code == 200, r.content
    body = r.json()
    assert body[block] is None
    assert "counts" in body and "gates" in body and "volunteer" in body
    [rec] = [x for x in caplog.records if x.name == "shelter.views"]
    assert block in rec.getMessage() and rec.exc_info is not None


@pytest.mark.django_db
def test_a_database_error_in_a_block_does_not_poison_the_rest_of_the_response(client, shelter, monkeypatch):
    # A failed query aborts a Postgres transaction; without a savepoint every query after it fails too.
    def bad_sql(_shelter):
        with connection.cursor() as cur:
            cur.execute("SELECT * FROM no_such_table")

    monkeypatch.setattr("shelter.views._rescue_summary", bad_sql)
    r = client.get(DASH, **_hdr(shelter))
    assert r.status_code == 200, r.content
    assert r.json()["rescue"] is None
    assert r.json()["wishlist"] is not None


@pytest.mark.django_db
def test_healthy_blocks_are_unchanged(client, shelter):
    body = client.get(DASH, **_hdr(shelter)).json()
    assert body["rescue"] is not None and body["wishlist"] is not None
