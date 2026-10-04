"""R2-F1 · the idempotency pre-check at the top of `ReportsCreateView.post` races against
a concurrent POST with the same `(reporter_account, idempotency_key)`. The outbox's retry
loop can fire two retries at once, both pass the pre-check (neither sees the other's row
yet), and the second insert hits `UniqueViolation` on `idx_stray_report_idempotency` → the
view returns a bare 500 with an `IntegrityError` traceback.

The replay guarantee is in the view's docstring ("Returning the EXISTING row rather than
creating a second is what stops a flaky connection from dispatching two rescuers to one
animal"), and the sequential replay already returns the 200 shape; this pins the CONCURRENT
replay to the same 200 shape — one 201 and one 200 (same report_id), never a 500, never two
rows, never two sets of side effects.

Observed in Sagip test plan Run 2 (`dev/test-plan-sagip.md` R2-F1); the device walk's outbox
retry triggered the race and the backend log recorded two 500s before the third POST hit the
pre-check cleanly.
"""
import threading

import pytest
from django.db import connection
from django.test import Client
from django.utils import timezone as tz

from accounts.factories import AccountFactory
from accounts.models import Address
from accounts.tokens import tokens_for
from notifications.models import Notification
from sagip.models import StrayReport
from verifications.models import AccountCapability


def _auth(account):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(account)['access']}"}


BODY = {"species": "dog", "condition": "injured", "lat": 14.6507, "lng": 121.1029,
        "city": "Marikina", "idempotency_key": "race-key"}


@pytest.mark.django_db(transaction=True)
def test_two_posts_with_the_same_idempotency_key_at_once_is_a_201_and_a_200_never_a_500():
    """Both POSTs are the same report (outbox retry). Expected: exactly one 201 creating the
    row, exactly one 200 replaying it, never a 500. Only one row ends up in the DB."""
    reporter = AccountFactory()
    client = Client()
    hdr = _auth(reporter)
    codes_bodies = []
    barrier = threading.Barrier(2)

    def hit():
        try:
            barrier.wait(timeout=5)
            r = client.post("/api/v1/reports", data=BODY, content_type="application/json", **hdr)
            try:
                codes_bodies.append((r.status_code, r.json()))
            except ValueError:
                codes_bodies.append((r.status_code, r.content[:200]))
        finally:
            connection.close()  # each thread holds its own DB connection

    threads = [threading.Thread(target=hit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    codes = sorted(c for c, _ in codes_bodies)
    assert codes == [200, 201], f"expected one 201 and one 200, got {codes_bodies!r}"

    # Same row id reported both times — the 200 is a replay of the 201's row.
    r201 = next(b for c, b in codes_bodies if c == 201)
    r200 = next(b for c, b in codes_bodies if c == 200)
    assert isinstance(r201, dict) and isinstance(r200, dict)
    assert r201["report_id"] == r200["report_id"]
    assert r200["status"] == "reported"

    # Exactly one row — never two.
    assert StrayReport.objects.filter(
        reporter_account=reporter, idempotency_key=BODY["idempotency_key"]).count() == 1


@pytest.mark.django_db(transaction=True)
def test_a_racing_pair_pages_the_city_only_once():
    """The 200 replay path returns before `alerts.alert_at_report`. So a 201 + 200 pair for
    one injured report pages a given recipient at most once; a 500-caused re-queue by the
    client would re-fire alerts on the next retry."""
    reporter = AccountFactory(phone="+639170000001", phone_verified_at=tz.now())  # D9
    rescuer = AccountFactory()
    AccountCapability.objects.create(account=rescuer, capability="rescuer", status="approved")
    Address.objects.create(account=rescuer, city="Marikina City", is_primary=True)

    client = Client()
    hdr = _auth(reporter)
    results = []
    barrier = threading.Barrier(2)

    def hit():
        try:
            barrier.wait(timeout=5)
            r = client.post("/api/v1/reports", data=BODY, content_type="application/json", **hdr)
            results.append(r.status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=hit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert 500 not in results, f"saw a 500: {results}"
    assert sorted(results) == [200, 201]

    # The verified Marikina rescuer should hear about the injured dog exactly once.
    paged = Notification.objects.filter(account=rescuer, type="report_nearby").count()
    assert paged == 1, f"expected one report_nearby, got {paged}"
