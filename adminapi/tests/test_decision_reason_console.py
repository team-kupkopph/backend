"""Review step 8 · the console's decision route takes a reason and a note (the model-level rules are in moderation/tests/test_decision_reason.py)."""
import pytest

from accounts.factories import AccountFactory
from adminapi.tests.conftest import full_signin
from listings.tests.adoption_helpers import listing, person
from moderation.models import ModerationFlag


@pytest.fixture
def auth(client, staffer):
    return {"HTTP_AUTHORIZATION": f"Bearer {full_signin(client, staffer)['access']}"}


def _flag(target_type, target_id):
    return ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type=target_type, target_id=target_id, reason="x")


@pytest.mark.django_db
def test_the_console_refuses_an_action_with_no_reason_and_accepts_one_with_it(client, auth):
    live = listing(person())
    flag = _flag("listing", live.pk)
    url = f"/admin-api/flags/{flag.flag_id}/action"
    no_reason = client.post(url, {}, content_type="application/json", **auth)
    assert no_reason.status_code == 422 and no_reason.json()["error"]["code"] == "reason_required"
    other_no_note = client.post(url, {"reason_code": "other"}, content_type="application/json", **auth)
    assert other_no_note.status_code == 422 and other_no_note.json()["error"]["code"] == "note_required"
    bad_type = client.post(url, {"reason_code": 5, "note": []}, content_type="application/json", **auth)
    assert bad_type.status_code == 422
    ok = client.post(url, {"reason_code": "harassment", "note": "see the thread"}, content_type="application/json", **auth)
    assert ok.status_code == 200
    body = ok.json()
    assert body["decision_reason_code"] == "harassment" and body["decision_note"] == "see the thread"
    live.refresh_from_db()
    assert live.hidden_at is not None


@pytest.mark.django_db
def test_the_console_takes_a_note_on_dismiss_and_none_is_fine(client, auth):
    a, b = _flag("listing", listing(person()).pk), _flag("listing", listing(person()).pk)
    with_note = client.post(f"/admin-api/flags/{a.flag_id}/dismiss", {"note": "fine"}, content_type="application/json", **auth)
    without = client.post(f"/admin-api/flags/{b.flag_id}/dismiss", {}, content_type="application/json", **auth)
    assert with_note.status_code == without.status_code == 200
    assert with_note.json()["decision_note"] == "fine" and without.json()["decision_note"] == ""
