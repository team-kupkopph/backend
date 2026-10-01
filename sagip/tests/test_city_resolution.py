"""C8 (dev/test-plan-sagip.md §0.3) · every city-scoped alert (report-time D2, level-1 escalation,
the re-alert) matches the report's `city` against people's primary-address city. The question was
what the phone actually sends.

Answered 2026-10-01 by reverse-geocoding every Metro Manila city centre through Apple's geocoder
(the service `expo-location`'s `reverseGeocodeAsync` calls on iOS, which reports CLPlacemark
`locality` as `city`). It always returns the CITY — barangays only ever appear in `subLocality`,
which the app doesn't send. The observed values are pinned below against the location picker's
spelling. The residual risk was a report with NO city (the geocoder failed — likely for a report
queued offline): nobody was paged at all. The server now derives the city from the point.
"""
import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from accounts.models import Address
from common.cities import canonical_city
from notifications.models import Notification
from sagip.geo import CITY_CENTROIDS, centroid_for, city_from_point
from sagip.models import StrayReport
from verifications.models import AccountCapability

# (Apple `locality`, observed 2026-10-01) -> (LocationPickerScreen value)
APPLE_VS_PICKER = [
    ("Marikina", "Marikina City"), ("Pasig", "Pasig City"), ("Quezon City", "Quezon City"),
    ("Manila", "Manila"), ("Makati", "Makati City"), ("Taguig", "Taguig City"),
    ("Pasay", "Pasay City"), ("Mandaluyong", "Mandaluyong City"), ("San Juan City", "San Juan"),
    ("Caloocan", "Caloocan City"), ("Parañaque", "Parañaque"), ("Las Piñas", "Las Piñas"),
    ("Muntinlupa", "Muntinlupa"), ("Valenzuela City", "Valenzuela"), ("Malabon City", "Malabon"),
    ("Navotas City", "Navotas"), ("Pateros", "Pateros"),
]


@pytest.mark.parametrize("apple,picker", APPLE_VS_PICKER)
def test_what_ios_sends_matches_what_people_picked(apple, picker):
    assert canonical_city(apple) == canonical_city(picker)


@pytest.mark.parametrize("apple,_picker", APPLE_VS_PICKER)
def test_the_map_can_search_every_metro_manila_city(apple, _picker):
    """The backend knew 10 city centres while the app's picker offers 17, so a Parañaque user
    was told the map "doesn't cover" their city (S15)."""
    assert centroid_for(apple) is not None


def test_a_point_resolves_to_its_city_and_nowhere_resolves_to_nothing():
    assert city_from_point(14.6507, 121.1029) == "Marikina"
    assert city_from_point(14.4793, 121.0198) == "Parañaque"
    assert city_from_point(10.3157, 123.8854) is None            # Cebu: no known centre near
    assert len(CITY_CENTROIDS) == 17


@pytest.mark.django_db
def test_a_report_with_no_city_still_pages_its_city():
    """The residual C8 hole: an offline-queued report whose reverse-geocode failed arrives with no
    city, and every city-scoped alert silently skipped it."""
    rescuer = AccountFactory()
    AccountCapability.objects.create(account=rescuer, capability="rescuer", status="approved")
    Address.objects.create(account=rescuer, city="Marikina City", is_primary=True)
    # D9 · alerts need a verified phone.
    c = APIClient(); c.force_authenticate(user=AccountFactory(phone_verified_at=timezone.now()))
    res = c.post("/api/v1/reports", {"species": "dog", "condition": "injured",
                                     "lat": 14.6507, "lng": 121.1029}, format="json")
    report = StrayReport.objects.get(pk=res.json()["report_id"])
    assert report.city == "Marikina"
    assert Notification.objects.filter(account=rescuer, type="report_nearby").exists()


@pytest.mark.django_db
def test_a_city_the_phone_did_send_is_kept_as_sent():
    c = APIClient(); c.force_authenticate(user=AccountFactory())
    res = c.post("/api/v1/reports", {"species": "dog", "condition": "healthy",
                                     "lat": 14.6507, "lng": 121.1029, "city": "Marikina City"},
                 format="json")
    assert StrayReport.objects.get(pk=res.json()["report_id"]).city == "Marikina City"
