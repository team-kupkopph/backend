import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from adminapi.tests.conftest import full_signin
from verifications.models import VerificationRequest


@pytest.fixture
def auth(client, staffer):
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}


@pytest.mark.django_db
def test_one_request_returns_every_count(client, auth):
    for _ in range(3):
        VerificationRequest.objects.create(
            account=AccountFactory(), type="shelter_org", status="pending")
    VerificationRequest.objects.create(
        account=AccountFactory(), type="shelter_org", status="approved")

    body = client.get("/admin-api/dashboard", **auth).json()
    assert body["pending_verifications"]["count"] == 3, "approved must not be counted as pending"
    assert set(body) == {"pending_verifications", "open_flags",
                         "unverified_donation_qrs", "new_members_this_week"}


@pytest.mark.django_db
def test_every_stat_carries_the_queue_it_opens(client, auth):
    """A number a reviewer cannot click is a number they cannot act on."""
    body = client.get("/admin-api/dashboard", **auth).json()
    for key, stat in body.items():
        assert stat["href"].startswith("/"), f"{key} has no destination"


@pytest.mark.django_db
def test_counts_are_exact_not_estimated(client, auth):
    """An approximate backlog is worse than a slow one — a reviewer who cannot trust the
    number has to open the queue anyway."""
    for _ in range(11):
        VerificationRequest.objects.create(
            account=AccountFactory(), type="shelter_org", status="pending")
    assert client.get("/admin-api/dashboard", **auth).json()["pending_verifications"]["count"] == 11


@pytest.mark.django_db
def test_the_dashboard_needs_a_staff_token(client):
    assert client.get("/admin-api/dashboard").status_code in (401, 403)
