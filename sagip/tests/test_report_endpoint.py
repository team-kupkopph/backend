"""US-S1 — POST /reports: anyone with an account can report a stray."""
import pytest

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from sagip.models import StrayReport


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


VALID = {
    "species": "dog", "condition": "injured", "is_anonymous": False,
    "lat": 14.63, "lng": 121.05, "location_text": "Near the Marikina bridge",
    "photos": [{"file_url": "https://example.invalid/1"},
               {"file_url": "https://example.invalid/2"}],
}


@pytest.mark.django_db
def test_a_guest_cannot_report_and_is_walled(client):
    res = client.post("/api/v1/reports", VALID, content_type="application/json")
    assert res.status_code == 401  # → the client raises the signup wall (US-A1b)


@pytest.mark.django_db
def test_an_authenticated_user_creates_a_report(client):
    acc = AccountFactory()
    res = client.post("/api/v1/reports", VALID, content_type="application/json", **_hdr(acc))
    assert res.status_code == 201
    body = res.json()
    assert body["status"] == "reported" and body["report_id"]
    r = StrayReport.objects.get(report_id=body["report_id"])
    assert r.reporter_account_id == acc.account_id
    assert r.species == "dog" and r.condition == "injured"
    assert r.status == "reported" and r.escalation_level == 0
    assert round(r.geom.y, 2) == 14.63 and round(r.geom.x, 2) == 121.05  # lat=y, lng=x
    assert r.location_text == "Near the Marikina bridge"
    assert r.photos.count() == 2


@pytest.mark.django_db
def test_client_resolved_city_is_stored_on_the_report(client):
    # US-S1 · the client passes the city it reverse-geocoded; the server stores the coarse label
    # so report-detail + the map show it (no server-side geocoder).
    acc = AccountFactory()
    res = client.post("/api/v1/reports", {**VALID, "city": "Marikina"},
                      content_type="application/json", **_hdr(acc))
    assert res.status_code == 201
    r = StrayReport.objects.get(report_id=res.json()["report_id"])
    assert r.city == "Marikina"


@pytest.mark.django_db
def test_report_without_a_city_stays_null(client):
    # No city (or blank) AND no known city near the point → NULL, so the map falls back to the
    # queried city and detail omits it. (C8: near a known city centre the server now derives the
    # city from the point instead — sagip/tests/test_city_resolution.py.)
    acc = AccountFactory()
    res = client.post("/api/v1/reports", {**VALID, "city": "  ", "lat": 10.3157, "lng": 123.8854},
                      content_type="application/json", **_hdr(acc))
    r = StrayReport.objects.get(report_id=res.json()["report_id"])
    assert r.city is None


@pytest.mark.django_db
def test_anonymous_report_still_records_the_reporter(client):
    acc = AccountFactory()
    res = client.post("/api/v1/reports", {**VALID, "is_anonymous": True},
                      content_type="application/json", **_hdr(acc))
    assert res.status_code == 201
    r = StrayReport.objects.get(report_id=res.json()["report_id"])
    assert r.is_anonymous is True
    assert r.reporter_account_id == acc.account_id  # anonymous hides from others, not the row


@pytest.mark.django_db
def test_coordinates_are_required(client):
    acc = AccountFactory()
    bad = {k: v for k, v in VALID.items() if k not in ("lat", "lng")}
    res = client.post("/api/v1/reports", bad, content_type="application/json", **_hdr(acc))
    assert res.status_code == 400


@pytest.mark.django_db
def test_out_of_range_coordinates_are_rejected(client):
    acc = AccountFactory()
    res = client.post("/api/v1/reports", {**VALID, "lat": 200},
                      content_type="application/json", **_hdr(acc))
    assert res.status_code == 400


@pytest.mark.django_db
def test_an_invalid_species_is_rejected(client):
    acc = AccountFactory()
    res = client.post("/api/v1/reports", {**VALID, "species": "dragon"},
                      content_type="application/json", **_hdr(acc))
    assert res.status_code == 400
