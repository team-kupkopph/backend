import pytest
from rest_framework.exceptions import Throttled, ValidationError

from common.errors import error_handler


def test_multi_field_validation_error_keeps_all_fields():
    exc = ValidationError({"email": ["required"], "password": ["too short"]})
    context = {"view": None, "request": None}

    response = error_handler(exc, context)

    error = response.data["error"]
    assert error["field"] == "email"
    assert error["message"] == "required"
    assert error["details"] == {
        "email": ["required"],
        "password": ["too short"],
    }


def test_throttled_exception_carries_retry_after_in_details():
    """US-SEC2 — DRF's default Throttled.__str__ embeds the wait in prose ("Expected
    available in 42 seconds"); the generic dict-with-"detail" branch would have kept
    that string but dropped exc.wait as structured data. This is the special case that
    makes the story's documented {code:"throttled", details:{retry_after}} shape real."""
    exc = Throttled(wait=42)
    context = {"view": None, "request": None}

    response = error_handler(exc, context)

    assert response.status_code == 429
    assert response.data == {
        "error": {
            "code": "throttled",
            "message": "Too many requests — try again shortly.",
            "details": {"retry_after": 42},
            # US-E2 · every error envelope now carries the correlation id the user can quote.
            # "-" outside a request, which is what this unit test is.
            "request_id": "-",
        }
    }


# -- K22 · a malformed id in the URL never falls through DRF to Django's HTML 404 -------
@pytest.mark.django_db
def test_an_api_404_from_url_routing_is_json(client):
    """A `<uuid:...>` path converter rejects a malformed id BEFORE DRF's view (and its
    exception handler) ever runs — Django's own URL resolver 404s first, straight past
    `error_handler` above, to the framework's HTML "Page not found" page. Every screen that
    reads `error.message` off that response fell back to generic copy."""
    res = client.get("/api/v1/shifts/not-a-uuid")
    assert res.status_code == 404
    assert res["Content-Type"].startswith("application/json")
    assert res.json()["error"]["code"] == "not_found"
    assert "request_id" in res.json()["error"]


def test_non_api_404s_are_untouched(client):
    res = client.get("/definitely-not-a-page")
    assert res.status_code == 404
    assert not res["Content-Type"].startswith("application/json")
