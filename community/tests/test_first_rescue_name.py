"""IB16 / IQ7 · the rescue badge is "First Rescue", not "Rescuer".

"Rescuer" already names a capability and a verified account (a Verified Member, a tier-1 shelter), so a badge called
Rescuer read as a standing the holder might not have. The badge is for a first rescue; it says so. Only the name moves:
the code, rule and every held badge are untouched.
"""
import pytest

from community.models import Badge

pytestmark = pytest.mark.django_db


def test_the_rescue_badge_is_called_first_rescue():
    assert Badge.objects.get(pk="first_rescue").name == "First Rescue"


def test_only_the_name_moved():
    b = Badge.objects.get(pk="first_rescue")
    assert (b.metric, b.threshold, b.sort_order) == ("rescues_helped", 1, 40)
    assert b.criteria == "1 rescue case helped"
