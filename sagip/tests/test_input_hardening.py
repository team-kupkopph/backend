"""C20 + C21 · input hardening: only link owned pets; clamp the map radius."""
import pytest
from django.contrib.gis.geos import Point
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from listings.models import Pet
from sagip.models import StrayReport


def _c(account):
    c = APIClient(); c.force_authenticate(user=account); return c


def _report(**kw):
    defaults = dict(reporter_account=AccountFactory(), species="dog", condition="injured",
                    status="reported", city="Marikina", geom=Point(121.10, 14.65, srid=4326))
    defaults.update(kw)
    return StrayReport.objects.create(**defaults)


@pytest.mark.django_db
def test_someone_elses_pet_is_not_linked_or_named():
    owner, stranger = AccountFactory(), AccountFactory()
    pet = Pet.objects.create(owner_account=owner, name="Bruno", species="dog")
    body = {"report_type": "lost", "species": "dog", "lat": 14.65, "lng": 121.10, "pet_id": str(pet.pk)}
    res = _c(stranger).post("/api/v1/reports", body, format="json")
    assert res.status_code == 201
    report = StrayReport.objects.get(pk=res.json()["report_id"])
    assert report.pet_id is None
    assert "pet_name" not in APIClient().get(f"/api/v1/reports/{report.pk}").json()


@pytest.mark.django_db
def test_a_pre_c20_row_holding_a_strangers_pet_does_not_name_it():
    """C20 · rows filed before the ownership check may still carry someone else's pet_id; the
    public detail only names a pet the report's own reporter owns."""
    owner = AccountFactory()
    theirs = Pet.objects.create(owner_account=owner, name="Bruno", species="dog")
    stranger_row = _report(report_type="lost", pet_id=theirs.pk)
    assert "pet_name" not in APIClient().get(f"/api/v1/reports/{stranger_row.pk}").json()

    own_row = _report(report_type="lost", reporter_account=owner, pet_id=theirs.pk)
    assert APIClient().get(f"/api/v1/reports/{own_row.pk}").json()["pet_name"] == "Bruno"


@pytest.mark.django_db
@pytest.mark.parametrize("radius", ["nan", "inf", "-5", "20000"])
def test_the_map_radius_is_clamped(radius):
    far = _report(city="Cebu City", geom=Point(123.89, 10.31, srid=4326))
    near = _report(geom=Point(121.1029, 14.6507, srid=4326))   # Marikina's centre
    res = APIClient().get(f"/api/v1/reports/map?city=Marikina&radius_km={radius}")
    assert res.status_code == 200
    ids = [r["report_id"] for r in res.json()["reports"]]
    assert str(far.pk) not in ids
    assert str(near.pk) in ids     # clamped, not zeroed: the map still shows the city itself
