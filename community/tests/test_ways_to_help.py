"""US-W4 · Ways to help: `GET /shelters/ways-to-help` — verified shelters asking for help, near the viewer first.

The list exists so someone with no shelter in mind can find one; a shelter with needs but no listing (EN-5) has no
other door. Design: library `docs/superpowers/specs/2026-10-27-us-w4-ways-to-help-design.md`.
"""
from datetime import timedelta

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.models import Address
from community.models import NeedStatus, ShelterNeed
from community.tests.test_needs import _need, _shelter
from shelter.models import DonationQr, ShelterProfile

KEYS = {"account_id", "org_name", "city", "near", "open_needs", "need_titles", "takes_qr"}


def _get(query=""):
    res = Client().get(f"/api/v1/shelters/ways-to-help{query}")
    assert res.status_code == 200
    return res.json()


def _ids(body):
    return [r["account_id"] for r in body["results"]]


def _at(shelter, city, primary=True):
    Address.objects.create(account=shelter, city=city, is_primary=primary)
    return shelter


def _qr(shelter, status="live"):
    return DonationQr.objects.create(account=shelter, provider="gcash", account_name="Shelter",
                                     qr_image_url="https://x/qr.png", status=status)


def _titled(shelter, title, minutes_ago=0, status=NeedStatus.OPEN):
    n = _need(shelter, status=status)
    ShelterNeed.objects.filter(pk=n.pk).update(title=title, created_at=timezone.now() - timedelta(minutes=minutes_ago))
    return n


@pytest.mark.django_db
def test_only_live_shelters_are_listed():
    with_need = _shelter()
    _need(with_need)
    qr_only = _shelter()
    _qr(qr_only)
    nothing_open = _shelter()
    _need(nothing_open, status=NeedStatus.FULFILLED)
    _need(nothing_open, status=NeedStatus.CLOSED)
    _qr(nothing_open, status="pending")
    unapproved = _shelter(approved=False)
    _need(unapproved)
    _qr(unapproved)
    suspended = _shelter(status="suspended")
    _need(suspended)
    _qr(suspended)

    assert set(_ids(_get())) == {str(with_need.pk), str(qr_only.pk)}


@pytest.mark.django_db
def test_row_shape():
    sh = _at(_shelter(display_name="Paws Account"), "Marikina")
    ShelterProfile.objects.create(account=sh, org_name="PAWS Marikina", org_type="shelter")
    _titled(sh, "Oldest", minutes_ago=30)
    _titled(sh, "Middle", minutes_ago=20)
    _titled(sh, "Newest", minutes_ago=10)
    _titled(sh, "Done", status=NeedStatus.FULFILLED)
    _qr(sh)
    bare = _shelter(display_name="No Profile Rescue")
    _need(bare)

    rows = {r["account_id"]: r for r in _get()["results"]}
    row = rows[str(sh.pk)]
    assert set(row) == KEYS
    assert row["org_name"] == "PAWS Marikina"
    assert row["city"] == "Marikina"
    assert row["open_needs"] == 3
    assert row["need_titles"] == ["Newest", "Middle"]
    assert row["takes_qr"] is True
    assert rows[str(bare.pk)]["org_name"] == "No Profile Rescue"
    assert rows[str(bare.pk)]["takes_qr"] is False


@pytest.mark.django_db
def test_near_matches_city_spelling_variants():
    marikina = _at(_shelter(), "Marikina")
    _need(marikina, )
    pasig = _at(_shelter(), "Pasig")
    _need(pasig)
    ShelterNeed.objects.filter(shelter_account=pasig).update(created_at=timezone.now())   # newer, but not near

    rows = _get("?city=Marikina%20City")["results"]
    assert [(r["account_id"], r["near"]) for r in rows] == [(str(marikina.pk), True), (str(pasig.pk), False)]


@pytest.mark.django_db
def test_a_shelter_with_no_address_is_listed_and_never_near():
    homeless = _shelter()
    _need(homeless)
    for query in ("", "?city=Marikina"):
        rows = {r["account_id"]: r for r in _get(query)["results"]}
        assert rows[str(homeless.pk)]["city"] == ""
        assert rows[str(homeless.pk)]["near"] is False


@pytest.mark.django_db
def test_no_city_means_nothing_is_near():
    _need(_at(_shelter(), "Marikina"))
    assert [r["near"] for r in _get()["results"]] == [False]
    assert [r["near"] for r in _get("?city=")["results"]] == [False]


@pytest.mark.django_db
def test_order_within_a_group():
    older = _at(_shelter(display_name="B Older"), "Marikina")
    _titled(older, "x", minutes_ago=60)
    newer = _at(_shelter(display_name="Z Newer"), "Marikina")
    _titled(newer, "x", minutes_ago=5)
    qr_only = _at(_shelter(display_name="A QR only"), "Marikina")
    _qr(qr_only)
    tie_b = _at(_shelter(display_name="D Tie"), "Marikina")
    tie_a = _at(_shelter(display_name="C Tie"), "Marikina")
    when = timezone.now() - timedelta(minutes=30)
    for sh in (tie_b, tie_a):
        ShelterNeed.objects.filter(pk=_need(sh).pk).update(created_at=when)

    assert _ids(_get("?city=Marikina")) == [str(newer.pk), str(tie_a.pk), str(tie_b.pk), str(older.pk), str(qr_only.pk)]


@pytest.mark.django_db
def test_pages_twenty_with_next():
    for _ in range(25):
        _need(_shelter())
    first = _get()
    second = _get("?page=2")
    assert (len(first["results"]), first["next"]) == (20, 2)
    assert (len(second["results"]), second["next"]) == (5, None)
    assert len(set(_ids(first)) | set(_ids(second))) == 25


@pytest.mark.django_db
def test_query_count_does_not_grow_with_shelters():
    def count(n):
        for _ in range(n):
            sh = _at(_shelter(), "Marikina")
            _need(sh)
            _need(sh)
            _qr(sh)
        with CaptureQueriesContext(connection) as ctx:
            _get("?city=Marikina")
        return len(ctx.captured_queries)

    small = count(3)
    assert count(9) == small


@pytest.mark.django_db
def test_the_primary_address_wins():
    sh = _at(_shelter(), "Pasig", primary=False)
    _at(sh, "Marikina")
    _need(sh)
    row = _get("?city=Marikina")["results"][0]
    assert (row["city"], row["near"]) == ("Marikina", True)
