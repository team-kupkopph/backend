"""S16 (dev/sagip-build-review.md) · the shelter's Home says what Sagip needs from it.

A verified shelter may claim strays and is paged about them (D2), but its shell had no rescue
surface at all — no count, no map entry, and a case it claimed was unreachable once it left the
screen. The dashboard now carries a `rescue` block the app turns into a Rescue card: the
shelter's city, how many strays need help near it (the SAME 10 km query the rescue map runs, so
the card and the map it opens never disagree), and how many of its own cases are open.
"""
import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from accounts.models import Address
from sagip.models import RescueCase, StrayReport
from shelter.models import ShelterProfile
from verifications.models import VerificationRequest

MARIKINA = Point(121.1029, 14.6507, srid=4326)
FAR = Point(123.8854, 10.3157, srid=4326)      # Cebu


def _shelter(city="Marikina City"):
    a = AccountFactory(account_type="shelter")
    VerificationRequest.objects.create(account=a, type="shelter_org", status="approved")
    ShelterProfile.objects.create(account=a, org_name="Paws", org_type="shelter", tier="registered_ngo")
    if city:
        Address.objects.create(account=a, city=city, is_primary=True)
    return a


def _report(**kw):
    fields = dict(species="dog", condition="injured", status="reported", geom=MARIKINA,
                  city="Marikina")
    fields.update(kw)
    return StrayReport.objects.create(reporter_account=AccountFactory(), **fields)


def _rescue(shelter):
    c = APIClient(); c.force_authenticate(user=shelter)
    res = c.get("/api/v1/shelter/dashboard")
    assert res.status_code == 200
    return res.json()["rescue"]


@pytest.mark.django_db
def test_counts_the_strays_that_need_help_near_the_shelter_like_the_map_does():
    shelter = _shelter()
    _report(); _report()
    _report(report_type="lost")                 # D6: a lost pet isn't a rescue job
    _report(status="claimed")                   # someone is already on it
    _report(geom=FAR, city="Cebu City")         # nowhere near
    rescue = _rescue(shelter)
    assert rescue["city"] == "Marikina City" and rescue["city_supported"] is True
    assert rescue["needs_help"] == 2

    c = APIClient()
    rows = c.get("/api/v1/reports/map?city=Marikina City&status=reported").json()["reports"]
    assert len([r for r in rows if r["report_type"] != "lost"]) == rescue["needs_help"]


@pytest.mark.django_db
def test_counts_the_shelters_own_open_cases_only():
    shelter = _shelter()
    RescueCase.objects.create(report=_report(status="claimed"), claimed_by_account=shelter)
    RescueCase.objects.create(report=_report(status="safe"), claimed_by_account=shelter)
    RescueCase.objects.create(report=_report(status="resolved"), claimed_by_account=shelter,
                              resolved_at=timezone.now())
    RescueCase.objects.create(report=_report(), claimed_by_account=shelter,
                              expired_at=timezone.now())                     # lapsed
    RescueCase.objects.create(report=_report(status="claimed"), claimed_by_account=AccountFactory())
    assert _rescue(shelter)["open_cases"] == 2


@pytest.mark.django_db
@pytest.mark.parametrize("city", [None, "Somewhere Unmapped"])
def test_a_city_the_map_cannot_search_is_said_plainly_not_counted_as_zero(city):
    """The 2026-09-04 lesson: "0 near you" over a city the map can't search is a false statement
    about whether an animal needs help. The count is null, and the app says why."""
    shelter = _shelter(city)
    _report()
    rescue = _rescue(shelter)
    assert rescue["city"] == city
    assert rescue["city_supported"] is False and rescue["needs_help"] is None
