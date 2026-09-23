"""P2 · what / where / who (review G1, G2, D5)."""
import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.models import Address
from shelter.models import ShelterProfile
from volunteer.models import SignupStatus, VolunteerShift, VolunteerSignup
from volunteer.tests.helpers import hdr, verified_shelter

SHIFTS = "/api/v1/shelter/shifts"


def _shelter_with_address(city="Marikina"):
    s = verified_shelter(display_name="KG Test Shelter")
    Address.objects.create(account=s, line1="12 Shelter Rd", barangay="Concepcion Uno",
                           city=city, province="Metro Manila", is_primary=True)
    ShelterProfile.objects.create(account=s, org_name="KG Test Shelter", org_type="shelter",
                                  tier="community_rescue", contact_person_name="Ana Reyes",
                                  official_phone="+639171112222",
                                  official_email="hello@kgshelter.invalid")
    return s


def _shift(shelter, **kw):
    start = timezone.now() + timezone.timedelta(days=2)
    d = dict(shelter_account=shelter, starts_at=start, ends_at=start + timezone.timedelta(hours=2),
             capacity=3, title="Morning dog walk", description="Walk two seniors. Bring water.",
             meeting_point="Front gate", address_line1="12 Shelter Rd",
             barangay="Concepcion Uno", city="Marikina", province="Metro Manila")
    d.update(kw)
    return VolunteerShift.objects.create(**d)


@pytest.mark.django_db
def test_a_shift_stores_what_and_where():
    s = _shift(_shelter_with_address())
    s.refresh_from_db()
    assert (s.title, s.city, s.meeting_point) == ("Morning dog walk", "Marikina", "Front gate")


@pytest.mark.django_db
def test_public_detail_shows_city_never_the_street(client):
    s = _shift(_shelter_with_address())
    body = client.get(f"/api/v1/shifts/{s.pk}").json()
    assert body["title"] == "Morning dog walk"
    assert body["description"].startswith("Walk two seniors")
    assert (body["city"], body["province"]) == ("Marikina", "Metro Manila")
    for hidden in ("location", "shelter_contact", "address_line1", "meeting_point"):
        assert hidden not in body


@pytest.mark.django_db
def test_a_requested_volunteer_sees_their_status_but_not_the_address(client):
    s = _shift(_shelter_with_address())
    vol = AccountFactory()
    su = VolunteerSignup.objects.create(shift=s, volunteer_account=vol, status=SignupStatus.REQUESTED)
    body = client.get(f"/api/v1/shifts/{s.pk}", **hdr(vol)).json()
    assert body["my_signup"] == {"signup_id": str(su.pk), "status": "requested"}
    assert body["viewer"] == {"needs_reapproval": False}
    assert "location" not in body and "shelter_contact" not in body


@pytest.mark.django_db
def test_an_approved_volunteer_sees_where_and_whom_to_contact(client):
    s = _shift(_shelter_with_address())
    vol = AccountFactory()
    VolunteerSignup.objects.create(shift=s, volunteer_account=vol, status=SignupStatus.APPROVED)
    body = client.get(f"/api/v1/shifts/{s.pk}", **hdr(vol)).json()
    assert body["location"] == {"meeting_point": "Front gate", "address_line1": "12 Shelter Rd",
                                "barangay": "Concepcion Uno", "city": "Marikina",
                                "province": "Metro Manila"}
    assert body["shelter_contact"] == {"name": "Ana Reyes", "phone": "+639171112222",
                                       "email": "hello@kgshelter.invalid"}


@pytest.mark.django_db
def test_my_signups_carries_location_only_for_approved_upcoming(client):
    shelter = _shelter_with_address()
    vol = AccountFactory()
    a = VolunteerSignup.objects.create(shift=_shift(shelter), volunteer_account=vol,
                                       status=SignupStatus.APPROVED)
    VolunteerSignup.objects.create(shift=_shift(shelter), volunteer_account=vol,
                                   status=SignupStatus.REQUESTED)
    body = client.get("/api/v1/me/signups", **hdr(vol)).json()
    up = body["upcoming"][0]["shift"]
    assert up["title"] == "Morning dog walk" and up["location"]["meeting_point"] == "Front gate"
    assert "location" not in body["requested"][0]["shift"]
    assert body["upcoming"][0]["signup_id"] == str(a.pk)


@pytest.mark.django_db
def test_the_owner_always_sees_their_own_location(client):
    shelter = _shelter_with_address()
    s = _shift(shelter)
    body = client.get(f"{SHIFTS}/{s.pk}", **hdr(shelter)).json()
    assert body["location"]["address_line1"] == "12 Shelter Rd"


def _post(client, shelter, **kw):
    body = {"type": "walking", "title": "Morning dog walk",
            "starts_at": "2030-10-04T09:00:00+08:00", "ends_at": "2030-10-04T11:00:00+08:00",
            "capacity": 2}
    body.update(kw)
    return client.post(SHIFTS, body, content_type="application/json", **hdr(shelter))


@pytest.mark.django_db
def test_location_defaults_to_the_shelters_primary_address(client):
    res = _post(client, _shelter_with_address())
    assert res.status_code == 201
    s = VolunteerShift.objects.get()
    assert (s.address_line1, s.city) == ("12 Shelter Rd", "Marikina")


@pytest.mark.django_db
def test_an_explicit_location_wins(client):
    _post(client, _shelter_with_address(), city="Pasig", address_line1="Rizal Park",
          meeting_point="By the fountain")
    s = VolunteerShift.objects.get()
    assert (s.city, s.address_line1, s.meeting_point) == ("Pasig", "Rizal Park", "By the fountain")


@pytest.mark.django_db
def test_no_address_anywhere_is_location_required(client):
    res = _post(client, verified_shelter())
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "location_required"


@pytest.mark.django_db
def test_title_is_required(client):
    res = _post(client, _shelter_with_address(), title="")
    assert res.status_code == 400
    assert res.json()["error"]["field"] == "title"


@pytest.mark.django_db
def test_browse_filters_by_city_case_insensitively(client):
    shelter = _shelter_with_address()
    m = _shift(shelter)
    _shift(shelter, city="Cebu City")
    ids = [r["shift_id"] for r in client.get("/api/v1/shifts?city=marikina").json()["results"]]
    assert ids == [str(m.pk)]

