"""Review steps 7g and 7c, console side: the category reaches the queue, and a reported person reaches their record."""
import pytest

from adminapi.tests.conftest import full_signin
from listings.tests.adoption_helpers import c, listing, person


@pytest.fixture
def auth(client, staffer):
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}


def _report(account, **body):
    return c(account).post("/api/v1/moderation/flags", body, format="json")


@pytest.mark.django_db
def test_the_console_rows_carry_the_category(client, auth):
    live = listing(person())
    flag_id = _report(person(), target_type="listing", target_id=str(live.pk), reason_code="harassment").json()["flag_id"]
    assert client.get(f"/admin-api/flags/{flag_id}", **auth).json()["reason_code"] == "harassment"
    assert client.get("/admin-api/flags", **auth).json()["results"][0]["reason_code"] == "harassment"


@pytest.mark.django_db
def test_a_reported_person_shows_up_on_their_member_record(client, auth):
    """The console already lists the flags about a member; before this nothing ever put one there."""
    poster = person()
    _report(person(), target_type="account", target_id=str(poster.pk), reason_code="harassment")
    assert len(client.get(f"/admin-api/members/{poster.pk}", **auth).json()["flags_about"]) == 1
