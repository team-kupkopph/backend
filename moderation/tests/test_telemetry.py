"""Review step 14 (MO20, MO21) · moderation reports what it did, and the audit row says what was decided about.

MO20: moderation emitted nothing, so volumes, the dismiss rate and time to decision could only come from ad hoc SQL.
The stream is ACCOUNT-FREE by design (common/analytics.py): kinds, categories, outcomes and coarse ages only, and sent only once the
transaction has committed, so a decision that rolls back reports nothing.
MO21: the audit row for a flag decision named the FLAG, so "who took down this listing?" needed a join to a table with no key to the content."""
import json
import logging

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from listings.tests.adoption_helpers import c, listing, person
from moderation import offenders, telemetry
from moderation.actions import TargetUnavailable, resolve_flag
from moderation.models import FlagStatus, ModerationFlag

FLAGS = "/api/v1/moderation/flags"
ALLOWED = {"event", "ts", "target_type", "reason_code", "about_a_person", "outcome", "took_down", "waited", "first_response",
           "system_raised", "rule"}


@pytest.fixture
def events(caplog, django_capture_on_commit_callbacks):
    caplog.set_level(logging.INFO, logger="kupkop.analytics")

    class Seen:
        def all(self):
            return [json.loads(r.getMessage()) for r in caplog.records if r.name == "kupkop.analytics"]

        def named(self, name):
            return [e for e in self.all() if e["event"] == name]

        def run(self, fn):
            with django_capture_on_commit_callbacks(execute=True):
                return fn()
    return Seen()


def _flag(target_type="listing", target_id=None, **kw):
    return ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type=target_type,
                                         target_id=target_id or listing(person()).pk, reason="x", **kw)


@pytest.mark.django_db
def test_a_report_emits_flag_created_with_coarse_dimensions_only(events):
    who, target = person(), listing(person())
    events.run(lambda: c(who).post(FLAGS, {"target_type": "listing", "target_id": str(target.pk), "reason_code": "spam_scam"}, format="json"))
    [e] = events.named("flag_created")
    assert e["target_type"] == "listing" and e["reason_code"] == "spam_scam" and e["about_a_person"] is False


@pytest.mark.django_db
def test_a_repeat_report_emits_flag_folded_not_a_second_created(events):
    who, target = person(), listing(person())
    for note in ("", "more"):
        events.run(lambda: c(who).post(FLAGS, {"target_type": "listing", "target_id": str(target.pk), "reason_code": "spam_scam", "reason": note},
                                       format="json"))
    assert len(events.named("flag_created")) == 1 and len(events.named("flag_folded")) == 1


@pytest.mark.django_db
def test_a_decision_emits_its_outcome_how_long_it_waited_and_whether_it_took_something_down(events):
    flag = _flag()
    ModerationFlag.objects.filter(pk=flag.pk).update(created_at=timezone.now() - timezone.timedelta(hours=30))
    events.run(lambda: resolve_flag(ModerationFlag.objects.get(pk=flag.pk), AccountFactory(), FlagStatus.ACTIONED, reason_code="harassment"))
    [e] = events.named("flag_decided")
    assert e["outcome"] == "actioned" and e["took_down"] is True and e["waited"] == "1_to_3d" and e["reason_code"] == "harassment"
    assert e["first_response"] is True and e["system_raised"] is False


@pytest.mark.django_db
def test_a_dismissal_says_nothing_was_taken_down_and_a_second_decision_is_not_a_first_response(events):
    flag = _flag()
    events.run(lambda: resolve_flag(flag, AccountFactory(), FlagStatus.REVIEWED))
    events.run(lambda: resolve_flag(ModerationFlag.objects.get(pk=flag.pk), AccountFactory(), FlagStatus.DISMISSED))
    first, second = events.named("flag_decided")
    assert (first["outcome"], first["first_response"]) == ("reviewed", True)
    assert (second["outcome"], second["took_down"], second["first_response"]) == ("dismissed", False, False)


@pytest.mark.django_db
def test_nothing_is_reported_until_the_transaction_commits(events, django_capture_on_commit_callbacks):
    """A decision that rolls back must report nothing, so the event waits for the commit."""
    flag = _flag()
    with django_capture_on_commit_callbacks() as callbacks:
        resolve_flag(flag, AccountFactory(), FlagStatus.DISMISSED)
    assert events.named("flag_decided") == []                     # decided in the transaction, not yet committed
    for callback in callbacks:                                    # the commit
        callback()
    assert len(events.named("flag_decided")) == 1


@pytest.mark.django_db
def test_a_decision_that_is_refused_reports_nothing(events):
    flag = _flag("listing", AccountFactory().pk)                                       # the listing does not exist
    with pytest.raises(TargetUnavailable):
        events.run(lambda: resolve_flag(flag, AccountFactory(), FlagStatus.ACTIONED, reason_code="spam_scam"))
    assert events.named("flag_decided") == []


@pytest.mark.django_db
def test_a_flag_the_system_raised_is_marked_as_such_and_reported(events):
    adopter = person()
    flag = events.run(lambda: offenders.raise_system_flag(adopter, offenders.RULE_WITHDRAWAL, "3 in a row"))
    [raised] = events.named("system_flag_raised")
    assert raised["rule"] == "repeat_withdrawal"
    events.run(lambda: resolve_flag(flag, AccountFactory(), FlagStatus.DISMISSED))
    assert events.named("flag_decided")[0]["system_raised"] is True


@pytest.mark.django_db
def test_no_event_carries_an_account_id_a_name_or_any_text(events):
    who, target = person(), listing(person(), name="Free puppies")
    events.run(lambda: c(who).post(FLAGS, {"target_type": "listing", "target_id": str(target.pk), "reason_code": "other",
                                           "reason": "he is a thief called Marco"}, format="json"))
    events.run(lambda: resolve_flag(ModerationFlag.objects.get(), AccountFactory(), FlagStatus.ACTIONED, reason_code="spam_scam", note="internal secret"))
    assert events.all()
    blob = json.dumps(events.all())
    for private in (str(who.pk), str(target.pk), who.display_name, "Marco", "thief", "internal secret", "Free puppies"):
        assert private not in blob
    for e in events.all():
        assert set(e) <= ALLOWED, set(e) - ALLOWED


def test_the_age_bands_are_coarse_and_ordered():
    assert [telemetry.age_bucket(h) for h in (0, 0.9, 1, 3.9, 4, 23.9, 24, 71.9, 72, 500)] == [
        "under_1h", "under_1h", "1_to_4h", "1_to_4h", "4_to_24h", "4_to_24h", "1_to_3d", "1_to_3d", "over_3d", "over_3d"]


@pytest.mark.django_db
def test_a_broken_analytics_stream_never_undoes_a_decision(monkeypatch, django_capture_on_commit_callbacks):
    """Plan row I6 · emit is fire-and-forget: a logging failure must not break the request that produced the event."""
    import common.analytics as analytics

    def boom(*a, **k):
        raise OSError("log pipeline down")
    monkeypatch.setattr(analytics.logger, "info", boom)
    flag = _flag()
    with django_capture_on_commit_callbacks(execute=True):
        resolve_flag(flag, AccountFactory(), FlagStatus.DISMISSED)
    flag.refresh_from_db()
    assert flag.status == FlagStatus.DISMISSED
