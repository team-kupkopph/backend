"""US-K2 · rate limits on the write paths that grew after Sprint 2 (§12.4).

Throttling was set up in US-SEC2 for the auth surface plus report/offer/flag creation. Three
sprints then added public write endpoints and none of them got a scope, so the abuse surface
roughly tripled without anybody deciding it should.

The gaps closed here, and why each one is worth a scope rather than a shrug:

  * `POST /stories`      — the newest public UGC surface. Unthrottled UGC is a spam feed.
  * `POST /media/presign`— hands out upload credentials. Unthrottled, it is free storage for
                           anyone with an account, and the cheapest possible amplification.
  * `POST /shelters/{id}/needs` and `POST /needs/{id}/pledges` — the wishlist loop. A pledge
                           is a promise a shelter plans around; a flood of them is a denial
                           of service against a shelter's ability to plan.

Each test drives the real endpoint until it 429s, following `common/tests/test_throttles.py`.
The rates live in settings, not here — they are policy, and these tests assert only that the
limit EXISTS and returns the documented envelope.
"""
import pytest
from django.core.cache import cache

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for


@pytest.fixture(autouse=True)
def _clear_throttle_history():
    # DRF stores throttle history in the cache; without this the scopes leak across tests
    # and whichever test ran second would see a 429 it did not cause.
    cache.clear()
    yield
    cache.clear()


def _auth(account):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(account)['access']}"}


def _limit_for(scope):
    """How many calls the configured rate allows, +1 to cross it.

    Derived from settings rather than hardcoded: the rates are policy and will be retuned
    against real traffic, and a test that pins a number would fail on a deliberate change
    while proving nothing about whether the limit works.
    """
    from django.conf import settings
    rate = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"][scope]
    return int(rate.split("/")[0]) + 1


def _statuses(call, times):
    return [call() for _ in range(times)]


@pytest.mark.django_db
def test_presign_is_throttled():
    account = AccountFactory()
    auth = _auth(account)

    from django.test import Client
    client = Client()
    codes = _statuses(
        lambda: client.post("/api/v1/media/presign",
                            {"purpose": "stray_photo", "content_type": "image/jpeg"},
                            content_type="application/json", **auth).status_code,
        _limit_for("media_presign"))

    assert 429 in codes, f"presign never throttled: {sorted(set(codes))}"


@pytest.mark.django_db
def test_story_creation_is_throttled():
    from django.test import Client
    account = AccountFactory()
    auth = _auth(account)
    client = Client()

    codes = _statuses(
        lambda: client.post("/api/v1/stories",
                            {"caption": "hello", "photos": [{"file_url": "https://x.invalid/a.jpg"}]},
                            content_type="application/json", **auth).status_code,
        _limit_for("story_create"))

    assert 429 in codes, f"story creation never throttled: {sorted(set(codes))}"


@pytest.mark.django_db
def test_pledging_is_throttled():
    from django.test import Client

    from community.models import ShelterNeed
    account = AccountFactory()
    need = ShelterNeed.objects.create(shelter_account=AccountFactory(), title="Dog food",
                                      category="food", quantity_needed=1000)
    auth = _auth(account)
    client = Client()

    codes = _statuses(
        lambda: client.post(f"/api/v1/needs/{need.pk}/pledges", {"quantity": 1},
                            content_type="application/json", **auth).status_code,
        _limit_for("pledge_create"))

    assert 429 in codes, f"pledging never throttled: {sorted(set(codes))}"


@pytest.mark.django_db
def test_a_throttled_response_uses_the_documented_error_envelope():
    """US-SEC2 gave throttling a specific shape (`code: "throttled"` plus
    `details.retry_after`). A new scope that returned DRF's raw `{"detail": ...}` would break
    every client's error handling, so the envelope is asserted, not assumed."""
    from django.test import Client
    account = AccountFactory()
    auth = _auth(account)
    client = Client()

    last = None
    for _ in range(_limit_for("media_presign")):
        res = client.post("/api/v1/media/presign",
                          {"purpose": "stray_photo", "content_type": "image/jpeg"},
                          content_type="application/json", **auth)
        if res.status_code == 429:
            last = res
            break

    assert last is not None
    body = last.json()["error"]
    assert body["code"] == "throttled"
    assert "retry_after" in body["details"]


@pytest.mark.django_db
def test_every_new_scope_has_a_configured_rate():
    """A `throttle_scope` with no matching rate in settings is silently NOT throttled — DRF
    treats a missing rate as unlimited. That failure is invisible, so it is asserted here."""
    from django.conf import settings

    rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    for scope in ("media_presign", "story_create", "need_create", "pledge_create"):
        assert rates.get(scope), f"scope {scope!r} has no configured rate"
