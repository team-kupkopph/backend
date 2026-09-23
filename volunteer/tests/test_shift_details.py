"""P2 · what / where / who (review G1, G2, D5)."""
import pytest
from django.utils import timezone

from accounts.models import Address
from shelter.models import ShelterProfile
from volunteer.models import VolunteerShift
from volunteer.tests.helpers import verified_shelter

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
