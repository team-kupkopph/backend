import pytest
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from pets.models import Pet, PetPhoto


@pytest.mark.django_db
def test_me_pets_returns_only_own():
    a = AccountFactory()
    b = AccountFactory()
    Pet.objects.create(owner_account=a, name="Rex", species="dog")
    Pet.objects.create(owner_account=b, name="Bo", species="cat")
    c = APIClient()
    c.force_authenticate(user=a)
    body = c.get("/api/v1/me/pets").json()
    assert [p["name"] for p in body["results"]] == ["Rex"]


@pytest.mark.django_db
def test_me_pets_guest_401():
    assert APIClient().get("/api/v1/me/pets").status_code == 401


@pytest.mark.django_db
def test_me_pets_photo_url_uses_pet_photo_url():
    a = AccountFactory()
    pet = Pet.objects.create(owner_account=a, name="Rex", species="dog")
    PetPhoto.objects.create(pet=pet, url="https://example.com/rex.jpg", is_primary=True)
    c = APIClient()
    c.force_authenticate(user=a)
    body = c.get("/api/v1/me/pets").json()
    assert body["results"][0]["photo_url"] == "https://example.com/rex.jpg"


@pytest.mark.django_db
def test_me_pets_photo_url_null_when_no_photo():
    a = AccountFactory()
    Pet.objects.create(owner_account=a, name="Rex", species="dog")
    c = APIClient()
    c.force_authenticate(user=a)
    body = c.get("/api/v1/me/pets").json()
    assert body["results"][0]["photo_url"] is None
