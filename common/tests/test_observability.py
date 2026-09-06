"""US-E2 · logging, request correlation, and the Sentry seam (§16.5).

§16.5 asks for structured JSON logs, error tracking, and request correlation. None of it was
configured: `common/analytics.py::emit` has been writing JSON lines since Sprint 6 with **no
`LOGGING` block to route them anywhere**, and there was no way to tie a user's error report
to the lines that produced it.

⚠️ THE PART THAT MATTERS MOST IS THE SCRUBBING, and it is why this file is mostly about
redaction rather than plumbing. Observability is the one system whose entire job is to copy
production data somewhere else — an error tracker holds request bodies, headers and local
variables by default. Shipping it unscrubbed would mean:

  * bearer tokens in a third-party dashboard, i.e. account takeover from a support ticket;
  * a stray report's **precise coordinates** leaving the system that deliberately withholds
    them from strangers (§12.5), through the back door;
  * emails and phone numbers in a service the RA 10173 privacy notice never mentions.

A breach notification caused by our own logging is an avoidable §12.6 incident, so the
redaction is asserted here rather than trusted.
"""
import json
import logging

import pytest

from common.observability import JsonFormatter, request_id_var, scrub


class TestScrubbing:
    def test_authorization_headers_are_redacted(self):
        event = {"request": {"headers": {"Authorization": "Bearer eyJhbGciOi.secret.value",
                                         "Content-Type": "application/json"}}}
        out = scrub(event)

        assert "eyJhbGciOi.secret.value" not in json.dumps(out)
        assert out["request"]["headers"]["Content-Type"] == "application/json"

    def test_password_and_token_fields_are_redacted_anywhere_they_appear(self):
        event = {"extra": {"body": {"email": "a@b.ph", "password": "hunter2",
                                    "refresh": "eyJ.refresh.token",
                                    "fcm_token": "device-token-abc"}}}
        raw = json.dumps(scrub(event))

        for secret in ("hunter2", "eyJ.refresh.token", "device-token-abc"):
            assert secret not in raw, f"leaked: {secret}"

    def test_precise_coordinates_are_redacted(self):
        # §12.5 · the API withholds a report's exact pin from everyone but the claimant.
        # An error event carrying lat/lng would hand it to a third-party dashboard instead.
        event = {"extra": {"payload": {"lat": 14.6349, "lng": 121.0509, "species": "dog"}}}
        out = scrub(event)

        assert out["extra"]["payload"]["lat"] == "[redacted]"
        assert out["extra"]["payload"]["lng"] == "[redacted]"
        assert out["extra"]["payload"]["species"] == "dog"   # coarse data survives

    def test_email_addresses_are_redacted_even_in_free_text(self):
        # They turn up in exception MESSAGES, not just in tidy fields.
        event = {"message": "IntegrityError for ana.reyes@example.ph on signup"}
        assert "ana.reyes@example.ph" not in json.dumps(scrub(event))

    def test_philippine_phone_numbers_are_redacted_in_free_text(self):
        event = {"message": "SMS to +639171234567 failed"}
        assert "+639171234567" not in json.dumps(scrub(event))

    def test_scrubbing_is_recursive(self):
        event = {"a": {"b": [{"c": {"password": "deep-secret"}}]}}
        assert "deep-secret" not in json.dumps(scrub(event))

    def test_ordinary_diagnostic_content_survives(self):
        # Over-scrubbing makes the tool useless: if every event is "[redacted]" nobody can
        # debug anything, and the tool gets removed.
        event = {"message": "ValueError in sagip.matching", "extra": {"report_type": "lost",
                                                                     "score_bucket": "high"}}
        out = scrub(event)

        assert out["message"] == "ValueError in sagip.matching"
        assert out["extra"]["report_type"] == "lost"

    def test_scrubbing_never_raises_on_odd_input(self):
        # A scrubber that throws takes down the error handler that called it, turning a
        # single failure into a total loss of error reporting.
        for weird in (None, 42, "plain", [1, 2], {"k": None}, {"k": object()}):
            scrub(weird)


class TestRequestId:
    def test_a_request_id_is_available_to_log_records(self):
        token = request_id_var.set("req-abc123")
        try:
            assert request_id_var.get() == "req-abc123"
        finally:
            request_id_var.reset(token)

    def test_it_defaults_to_a_dash_outside_a_request(self):
        # Sweeps and management commands log too, and a missing key would break the JSON
        # formatter for exactly the runs nobody is watching.
        assert request_id_var.get() == "-"


@pytest.mark.django_db
def test_the_error_envelope_carries_the_request_id(client):
    """§16.5 · request correlation. A user reporting "it failed at 3pm" is unactionable; a
    user reading back an id from the error they saw points straight at the log lines."""
    from accounts.factories import AccountFactory
    from accounts.tokens import tokens_for

    account = AccountFactory()
    res = client.delete("/api/v1/me", {}, content_type="application/json",
                        HTTP_AUTHORIZATION=f"Bearer {tokens_for(account)['access']}")

    assert res.status_code == 400
    assert res["X-Request-ID"]                      # echoed for the user to quote
    assert res.json()["error"].get("request_id") == res["X-Request-ID"]


@pytest.mark.django_db
def test_an_inbound_request_id_is_honoured(client):
    """A load balancer or client that already assigned one must win, or the id changes at
    every hop and correlates nothing."""
    res = client.get("/api/v1/health", HTTP_X_REQUEST_ID="from-the-edge-1")
    assert res["X-Request-ID"] == "from-the-edge-1"


def test_the_json_formatter_emits_one_parseable_object_per_line():
    from common.observability import JsonFormatter

    record = logging.LogRecord("kupkop.analytics", logging.INFO, __file__, 10,
                               '{"event": "report_created"}', None, None)
    line = JsonFormatter().format(record)
    parsed = json.loads(line)

    assert parsed["logger"] == "kupkop.analytics"
    assert parsed["level"] == "INFO"
    assert "ts" in parsed and "request_id" in parsed
    # An already-JSON analytics payload is nested, not double-encoded into a string — a
    # pipeline should be able to query `event` directly.
    assert parsed["message"]["event"] == "report_created"


def test_the_formatter_scrubs_what_it_logs():
    from common.observability import JsonFormatter

    record = logging.LogRecord("kupkop.api", logging.ERROR, __file__, 10,
                               "login failed for ana@example.ph", None, None)
    assert "ana@example.ph" not in JsonFormatter().format(record)


def test_sentry_is_a_no_op_until_it_is_configured(settings):
    """The FCM/S3 posture: the seam is real and tested, the credential is a deploy-time
    task, and an unset DSN must not be an error on every boot."""
    from common.observability import init_sentry

    settings.SENTRY_DSN = ""
    assert init_sentry() is False


def test_a_dict_logged_directly_is_nested_not_repr_ed():
    """`logger.warning({"event": ...})` is the natural way to log structured data, and it
    was silently producing an UNPARSEABLE line.

    `record.getMessage()` renders a dict with `str()`, so the payload arrived as Python
    repr — single quotes, `None` instead of `null` — which starts with `{` and therefore
    looks like JSON to a reader while failing every JSON parser. The whole point of §16.5's
    structured logging is that a pipeline can filter on `message.event`; a repr blob defeats
    that while looking exactly like it works. Found by reading a real log line from the
    overlap guard, not by a test of the formatter.
    """
    record = logging.LogRecord("kupkop.locks", logging.WARNING, __file__, 1,
                               {"event": "scheduled_run_skipped", "command": "run_sweeps"},
                               None, None)
    line = json.loads(JsonFormatter().format(record))
    assert line["message"] == {"event": "scheduled_run_skipped", "command": "run_sweeps"}


def test_a_logged_dict_is_scrubbed_like_everything_else():
    # A dict is the easiest way to log a whole object by accident; it must not become the
    # one path into the log stream that skips redaction.
    record = logging.LogRecord("kupkop.x", logging.INFO, __file__, 1,
                               {"email": "ana@example.ph", "lat": 14.6, "species": "dog"},
                               None, None)
    line = json.loads(JsonFormatter().format(record))
    assert line["message"]["email"] == "[redacted]"
    assert line["message"]["lat"] == "[redacted]"
    assert line["message"]["species"] == "dog"
