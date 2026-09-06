import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for


@pytest.mark.django_db
def test_put_location_upserts_city_without_coordinate(client):
    acc = AccountFactory(email_verified_at=timezone.now())
    hdr = {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}
    res = client.put("/api/v1/me/location", {"city": "Marikina", "barangay": "Sto. Niño"},
                     content_type="application/json", **hdr)
    assert res.status_code == 200
    assert res.json() == {"city": "Marikina", "barangay": "Sto. Niño"}
    from accounts.models import Address
    addr = Address.objects.get(account=acc, is_primary=True)
    assert addr.city == "Marikina" and addr.geom is None    # decision 11: no person coordinate
