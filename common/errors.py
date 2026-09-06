from rest_framework.exceptions import Throttled
from rest_framework.views import exception_handler

from common.observability import request_id_var


def _shape_error(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return response
    # US-SEC2 · a Throttled exception's DRF response is {"detail": "..."} like any other
    # APIException, so it would otherwise fall into the generic branch below and lose
    # `exc.wait` (the seconds-to-retry DRF already computed) — special-cased so the story's
    # documented shape (`details: {retry_after}`) is real, not aspirational.
    if isinstance(exc, Throttled):
        response.data = {"error": {"code": "throttled",
                                   "message": "Too many requests — try again shortly.",
                                   "details": {"retry_after": exc.wait}}}
        return response
    detail = response.data
    code = getattr(exc, "default_code", "error")
    message = ""
    if isinstance(detail, dict) and "detail" in detail:
        message = str(detail["detail"])
        code = getattr(detail["detail"], "code", code)
    elif isinstance(detail, dict):
        field, msgs = next(iter(detail.items()))
        message = msgs[0] if isinstance(msgs, list) else str(msgs)
        details = {
            f: (m if isinstance(m, list) else [m])
            for f, m in detail.items()
        }
        response.data = {
            "error": {
                "code": code,
                "message": message,
                "field": field,
                "details": details,
            }
        }
        return response
    response.data = {"error": {"code": code, "message": message}}
    return response


def error_handler(exc, context):
    """The DRF exception handler: shape the envelope, then stamp the correlation id.

    US-E2 · the id goes in the error the USER sees. "It failed around 3pm" is unactionable;
    an id read back off the screen points support at the exact log lines. Stamped here, at
    the single exit, because `_shape_error` has four returns and the fifth one someone adds
    would otherwise forget.
    """
    return _stamp_request_id(_shape_error(exc, context))


def _stamp_request_id(response):
    if response is not None and isinstance(getattr(response, "data", None), dict):
        error = response.data.get("error")
        if isinstance(error, dict):
            error["request_id"] = request_id_var.get()
    return response
