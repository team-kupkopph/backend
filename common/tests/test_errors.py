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
