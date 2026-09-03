"""US-L1 · filing a lost/found report through POST /reports (intake for the §11 matcher)."""
import pytest
from django.contrib.gis.geos import Point

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from listings.models import Pet
from sagip.models import ReportMatch, ReportType, StrayReport


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


def _body(**over):
    body = {"species": "dog", "condition": "healthy", "lat": 14.65, "lng": 121.10}
    body.update(over)
    return body


@pytest.mark.django_db
def test_file_a_lost_report_with_describables(client):
    res = client.post("/api/v1/reports",
                      _body(report_type="lost", breed="aspin", color_markings="brown",
                            size_category="medium", sex="male"),
                      content_type="application/json", **_hdr(AccountFactory()))
    assert res.status_code == 201
    r = StrayReport.objects.get(pk=res.json()["report_id"])
    assert r.report_type == ReportType.LOST and r.breed == "aspin"
    assert r.size_category == "medium" and r.sex == "male"


@pytest.mark.django_db
def test_report_type_defaults_to_stray(client):
    res = client.post("/api/v1/reports", _body(),
                      content_type="application/json", **_hdr(AccountFactory()))
    r = StrayReport.objects.get(pk=res.json()["report_id"])
    assert r.report_type == ReportType.STRAY


@pytest.mark.django_db
def test_a_lost_report_prefills_describables_from_the_owners_pet(client):
    owner = AccountFactory()
    pet = Pet.objects.create(owner_account=owner, name="Rex", species="dog", breed="aspin",
                             color_markings="brown", size_category="large", sex="male")
    res = client.post("/api/v1/reports",
                      _body(report_type="lost", pet_id=str(pet.pk)),   # no describables passed
                      content_type="application/json", **_hdr(owner))
    r = StrayReport.objects.get(pk=res.json()["report_id"])
    assert r.breed == "aspin" and r.size_category == "large"
    assert r.pet_id == pet.pk


@pytest.mark.django_db
def test_another_users_pet_id_does_not_prefill(client):
    stranger = AccountFactory()
    pet = Pet.objects.create(owner_account=AccountFactory(), name="Rex", species="dog",
                             breed="secret")
    res = client.post("/api/v1/reports",
                      _body(report_type="lost", pet_id=str(pet.pk)),
                      content_type="application/json", **_hdr(stranger))
    r = StrayReport.objects.get(pk=res.json()["report_id"])
    assert r.breed is None                       # not prefilled from someone else's pet


@pytest.mark.django_db
def test_filing_the_opposite_report_creates_a_match(client):
    """End-to-end: an existing lost report, then a found report filed nearby -> a match row."""
    finder = AccountFactory()
    # existing lost report (same species, close by)
    StrayReport.objects.create(report_type=ReportType.LOST, species="dog", condition="healthy",
                               breed="aspin", geom=Point(121.10, 14.65, srid=4326),
                               reporter_account=AccountFactory())
    res = client.post("/api/v1/reports",
                      _body(report_type="found", breed="aspin", lat=14.651, lng=121.101),
                      content_type="application/json", **_hdr(finder))
    assert res.status_code == 201
    found = StrayReport.objects.get(pk=res.json()["report_id"])
    assert ReportMatch.objects.filter(report=found).exists()
