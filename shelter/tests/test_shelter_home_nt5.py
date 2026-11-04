"""NT-5 (shelter home test plan) · the backend rows the review asked for that had no test yet.

- U16 · a tie on `created_at` across the page-20 boundary: every row exactly once, none missing.
- U18 · the dashboard for a shelter with no primary address, and for one with no profile at all.
- I1  · the dashboard's response shape, key for key (the inbox's is in test_requests_inbox_sql.py).
- I2  · the dashboard's query count does not grow with history (50 vs 500 rows), under a budget.
- I3  · a database error mid-dashboard: a 500 in the JSON envelope with the request id, logged once, nothing written.
- F10 · un-suspending a counterparty clears the inbox flag and the 409 with no data change.
"""
import logging
from datetime import timedelta

import pytest
from django.db import OperationalError, connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.models import AccountStatus
from accounts.tokens import tokens_for
from listings.models import AdoptionInquiry, AdoptionListing, InquiryStatus
from notifications.models import Notification
from shelter.models import ShelterProfile

INBOX = "/api/v1/shelter/requests"
DASH = "/api/v1/shelter/dashboard"


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _shelter(profile=True):
    acc = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    if profile:
        ShelterProfile.objects.create(account=acc, org_name="Paws", org_type="shelter")
    return acc


def _history(shelter, n, status=InquiryStatus.DECLINED, at=None):
    """`n` inquiries on the shelter's listings, one per (listing, adopter), all stamped `at` when given."""
    adopters = [AccountFactory(account_type="personal") for _ in range(min(n, 25))]
    listings = AdoptionListing.objects.bulk_create([
        AdoptionListing(posted_by=shelter, name=f"L{i}", species="dog", city="Marikina", adoption_fee="0")
        for i in range(-(-n // len(adopters)))])
    rows = AdoptionInquiry.objects.bulk_create([
        AdoptionInquiry(listing=listings[i // len(adopters)], adopter_account=adopters[i % len(adopters)],
                        status=status) for i in range(n)])
    if at is not None:
        AdoptionInquiry.objects.filter(pk__in=[r.pk for r in rows]).update(created_at=at)
    return rows


@pytest.mark.django_db
def test_u16_a_tie_across_the_page_boundary_gives_every_row_exactly_once(client):
    shelter = _shelter()
    tied = _history(shelter, 30, status=InquiryStatus.ACTIVE, at=timezone.now() - timedelta(hours=1))
    p1 = client.get(INBOX, {"status": "open"}, **_hdr(shelter)).json()
    p2 = client.get(INBOX, {"status": "open", "page": 2}, **_hdr(shelter)).json()
    ids = [r["id"] for r in p1["results"] + p2["results"]]
    assert (len(p1["results"]), p1["next"], len(p2["results"]), p2["next"]) == (20, 2, 10, None)
    assert len(ids) == len(set(ids)) == 30                       # none twice
    assert set(ids) == {str(r.pk) for r in tied}                 # none missing
    assert ids == sorted(ids, reverse=True)                      # the id tie-break holds across the boundary


@pytest.mark.django_db
class TestU18DashboardEdges:
    def test_no_primary_address_says_no_city_never_zero(self, client):
        body = client.get(DASH, **_hdr(_shelter())).json()
        assert body["rescue"]["city"] is None
        assert body["rescue"]["needs_help"] is None

    def test_no_shelter_profile_at_all_still_loads_with_zero_counts(self, client):
        r = client.get(DASH, **_hdr(_shelter(profile=False)))
        assert r.status_code == 200, r.content
        body = r.json()
        assert body["verification"]["submitted"] is False
        assert body["counts"]["draft_listings"] == body["counts"]["live_listings"] == body["counts"]["adopted"] == 0


@pytest.mark.django_db
def test_i1_the_dashboard_shape_key_for_key(client):
    body = client.get(DASH, **_hdr(_shelter())).json()
    assert set(body) == {"rescue", "verification", "counts", "gates", "volunteer", "wishlist"}
    assert set(body["verification"]) == {"submitted", "status", "docs"}
    assert set(body["counts"]) == {"draft_listings", "live_listings", "adopted", "volunteers", "donations"}
    assert set(body["gates"]) == {"can_publish", "donations_enabled"}
    assert set(body["volunteer"]) == {"pending_requests", "attendance_due", "next_shift", "active_shifts"}
    assert set(body["rescue"]) == {"city", "city_supported", "needs_help", "open_cases"}
    assert set(body["wishlist"]) == {"open_needs", "pledges_awaiting_confirm"}


@pytest.mark.django_db
def test_i2_the_dashboard_query_count_does_not_grow_with_history(client):
    # Ratchet: the count measured when this was written (the review guessed 18 before steps 13 and 15 added two
    # savepoints and `active_shifts`). It may fall; raising it is a decision.
    budget = 20
    small, large = _shelter(), _shelter()
    _history(small, 50)
    _history(large, 500)

    def queries(acc):
        hdr = _hdr(acc)
        with CaptureQueriesContext(connection) as ctx:
            assert client.get(DASH, **hdr).status_code == 200
        return len(ctx.captured_queries)

    at_50, at_500 = queries(small), queries(large)
    assert at_50 == at_500, (at_50, at_500)
    assert at_500 <= budget, at_500


@pytest.mark.django_db
def test_i3_a_database_error_mid_dashboard_is_a_json_500_logged_once_with_nothing_written(caplog):
    shelter = _shelter()
    before = Notification.objects.count()
    seen = {"n": 0}

    def drop_second_query(execute, sql, params, many, context):
        seen["n"] += 1
        if seen["n"] == 2:
            raise OperationalError("server closed the connection unexpectedly")
        return execute(sql, params, many, context)

    with caplog.at_level(logging.ERROR, logger="django.request"), connection.execute_wrapper(drop_second_query):
        r = Client(raise_request_exception=False).get(DASH, **_hdr(shelter))
    assert r.status_code == 500
    assert r["Content-Type"].startswith("application/json"), r.content[:120]
    err = r.json()["error"]
    assert err["code"] == "server_error" and err["request_id"]
    assert "OperationalError" not in r.content.decode()           # no internals in the body
    assert len([x for x in caplog.records if x.name == "django.request"]) == 1
    assert Notification.objects.count() == before


@pytest.mark.django_db
def test_f10_unsuspending_clears_the_flag_and_the_refusal_with_no_data_change(client):
    from listings.tests.adoption_helpers import inquire, listing, person, post
    from listings.tests.adoption_helpers import shelter as poster
    the_shelter, adopter = poster(), person()
    inq = inquire(adopter, listing(the_shelter))
    hdr = _hdr(the_shelter)

    adopter.status = AccountStatus.SUSPENDED
    adopter.save(update_fields=["status"])
    assert client.get(INBOX, {"kind": "adoption"}, **hdr).json()["results"][0]["counterparty_suspended"] is True
    assert post(the_shelter, inq, "screen").status_code == 409
    snapshot = AdoptionInquiry.objects.values().get(pk=inq.pk)

    adopter.status = AccountStatus.ACTIVE
    adopter.save(update_fields=["status"])
    assert AdoptionInquiry.objects.values().get(pk=inq.pk) == snapshot          # nothing was rewritten
    assert client.get(INBOX, {"kind": "adoption"}, **hdr).json()["results"][0]["counterparty_suspended"] is False
    assert post(the_shelter, inq, "screen").status_code == 200
