import pytest


@pytest.mark.django_db
def test_one_character_name_is_400_on_display_name(client):
    res = client.post("/api/v1/auth/signup", {"account_type": "personal", "display_name": "A",
                      "email": "a@kupkop.invalid", "password": "Pass1234", "consent_version": "2026-08-01"},
                      content_type="application/json")
    assert res.status_code == 400
    # NOTE: brief asserted res.json()["error"]["field"] == "display_name", but SignupView
    # (accounts/views.py) builds its 400 response by hand — {"error": {"code": "invalid",
    # "message": "Invalid input", "details": serializer.errors}} — rather than going through
    # the raise_exception=True path that common/errors.py._shape_error shapes into a
    # top-level "field" key. Every existing signup 400 test (accounts/tests/test_signup.py)
    # confirms this: none of them assert "field" for this endpoint. Substituted the assertion
    # to match SignupView's established (and out-of-scope-to-change) error contract.
    assert "display_name" in res.json()["error"]["details"]
