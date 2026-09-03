"""US-Y1 · server-side, account-free analytics (D-S6-6)."""
import json
import logging

from common.analytics import emit


def test_emit_writes_a_json_line_with_event_and_timestamp(caplog):
    with caplog.at_level(logging.INFO, logger="kupkop.analytics"):
        emit("report_created", type="lost", species="dog")
    rec = [r for r in caplog.records if r.name == "kupkop.analytics"][-1]
    payload = json.loads(rec.getMessage())
    assert payload["event"] == "report_created"
    assert payload["type"] == "lost" and payload["species"] == "dog"
    assert "ts" in payload


def test_emit_is_fire_and_forget(monkeypatch):
    """D-S6-6 / the send_push posture: a logging failure must never break the caller."""
    import common.analytics as a

    def boom(*args, **kwargs):
        raise RuntimeError("logging pipeline down")

    monkeypatch.setattr(a.logger, "info", boom)
    emit("anything", foo=1)   # must not raise
