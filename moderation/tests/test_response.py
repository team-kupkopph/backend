"""Review steps 22, 23 and 24 (MQ7, MQ8, MQ9) · what happens after a report is filed: the reporter hears it was looked at, staff
hear when it is late, and the words are not kept forever.

Apple 1.2 asks for "timely responses to concerns"; neither store names a number. These hold the three behaviours that make a
response real: the reporter is told WITHOUT the outcome, staff are told ONCE and never with names or words in the mail, and a
dismissed report's text is scrubbed but an upheld one's is not."""
import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from accounts.factories import AccountFactory
from listings.tests.adoption_helpers import listing, person
from moderation import notices, retention, sweeps
from moderation.actions import resolve_flag
from moderation.models import FlagStatus, ModerationFlag
from notifications.models import Notification

HOURS = 3600


def _flag(reporter=None, target=None, status=FlagStatus.OPEN, code="harassment", reason="words", age_hours=0, decided_days_ago=None):
    flag = ModerationFlag.objects.create(
        reporter_account=reporter if reporter is not None else AccountFactory(),
        target_type="listing", target_id=(target or listing(person())).pk, reason=reason, reason_code=code, status=status)
    created = timezone.now() - timezone.timedelta(hours=age_hours)
    fields = {"created_at": created}
    if decided_days_ago is not None:
        fields["reviewed_at"] = timezone.now() - timezone.timedelta(days=decided_days_ago)
    ModerationFlag.objects.filter(pk=flag.pk).update(**fields)
    flag.refresh_from_db()
    return flag


# ---- step 22 · the reporter hears it was looked at, and nothing about what was decided ---------------------------------
@pytest.mark.django_db
@pytest.mark.parametrize("decision", ["actioned", "dismissed"])
def test_the_reporter_is_told_their_report_was_looked_at(decision):
    reporter = AccountFactory()
    flag = _flag(reporter)
    resolve_flag(flag, AccountFactory(), decision)
    note = Notification.objects.get(account=reporter, type="report_reviewed")
    assert note.title == notices.TITLE and note.body == notices.BODY and note.data == {"flag_id": str(flag.pk)}


@pytest.mark.django_db
def test_the_notice_is_the_same_whatever_was_decided_so_it_cannot_leak_the_outcome():
    a, b = AccountFactory(), AccountFactory()
    resolve_flag(_flag(a), AccountFactory(), "actioned")
    resolve_flag(_flag(b), AccountFactory(), "dismissed")
    first, second = (Notification.objects.get(account=x, type="report_reviewed") for x in (a, b))
    assert (first.title, first.body) == (second.title, second.body)
    text = f"{first.title} {first.body}".lower()
    for word in ("removed", "dismissed", "actioned", "no action", "taken down", "hidden"):
        assert word not in text


@pytest.mark.django_db
def test_reviewed_is_a_working_state_and_says_nothing_yet():
    reporter = AccountFactory()
    resolve_flag(_flag(reporter), AccountFactory(), "reviewed")
    assert not Notification.objects.filter(type="report_reviewed").exists()


@pytest.mark.django_db
def test_every_reporter_whose_report_was_closed_with_the_target_is_told_once():
    target = listing(person())
    first, second, third = AccountFactory(), AccountFactory(), AccountFactory()
    main, sibling, reviewed = _flag(first, target), _flag(second, target), _flag(third, target, FlagStatus.REVIEWED)
    resolve_flag(main, AccountFactory(), "actioned")
    for who in (first, second, third):
        assert Notification.objects.filter(account=who, type="report_reviewed").count() == 1
    assert sibling.pk and reviewed.pk


@pytest.mark.django_db
def test_a_system_flag_has_nobody_to_tell(caplog):
    flag = ModerationFlag.objects.create(reporter_account=None, target_type="account", target_id=AccountFactory().pk, reason="rule")
    resolve_flag(flag, AccountFactory(), "dismissed")
    assert not Notification.objects.filter(type="report_reviewed").exists()
    assert "could not tell" not in caplog.text          # skipped on purpose, not by catching a crash


@pytest.mark.django_db
def test_a_notice_that_fails_does_not_undo_the_decision(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("push provider down")
    monkeypatch.setattr(notices, "notify", boom)
    flag = _flag()
    resolve_flag(flag, AccountFactory(), "dismissed")
    flag.refresh_from_db()
    assert flag.status == FlagStatus.DISMISSED


@pytest.mark.django_db
def test_a_takedown_tells_the_poster_how_to_ask_for_a_second_look():
    poster = person()
    flag = _flag(target=listing(poster))
    resolve_flag(flag, AccountFactory(), "actioned")
    body = Notification.objects.get(account=poster, type="listing_removed").body
    assert notices.appeal_line() in body and "hello@kupkop.ph" in body


@override_settings(SUPPORT_EMAIL="help@example.org")
def test_the_appeal_address_is_one_setting():
    assert notices.appeal_line() == "If you think this was a mistake, write to help@example.org."


# ---- step 23 · staff hear when a report is late, once ---------------------------------------------------------------
@override_settings(MODERATION_ALERT_EMAILS=["ops@example.org"])
@pytest.mark.django_db
def test_a_report_past_its_target_is_alerted_once_not_every_hour(capsys):
    late = _flag(age_hours=25)
    assert sweeps.alert_overdue_flags() == [late]
    late.refresh_from_db()
    assert late.alerted_at is not None
    assert sweeps.alert_overdue_flags() == []                 # the next hourly run has nothing new to say


@override_settings(MODERATION_ALERT_EMAILS=["ops@example.org"])
@pytest.mark.django_db
def test_an_animal_in_danger_is_late_after_four_hours_and_the_rest_after_a_day():
    welfare_3h, welfare_5h = _flag(code="animal_welfare", age_hours=3), _flag(code="animal_welfare", age_hours=5)
    other_5h, other_25h = _flag(age_hours=5), _flag(age_hours=25)
    assert set(sweeps.overdue_flags()) == {welfare_5h, other_25h}
    assert welfare_3h not in sweeps.overdue_flags() and other_5h not in sweeps.overdue_flags()


@override_settings(MODERATION_ALERT_EMAILS=["ops@example.org"])
@pytest.mark.django_db
def test_only_open_reports_are_waiting_for_a_first_look():
    for status in (FlagStatus.REVIEWED, FlagStatus.ACTIONED, FlagStatus.DISMISSED):
        _flag(status=status, age_hours=100)
    assert sweeps.overdue_flags() == []


@override_settings(MODERATION_ALERT_EMAILS=[])
@pytest.mark.django_db
def test_with_nobody_to_tell_nothing_is_marked_alerted(caplog):
    """Stamping a flag 'alerted' when no one was told would hide it from the one alert that exists."""
    late = _flag(age_hours=30)
    assert sweeps.alert_overdue_flags() == []
    late.refresh_from_db()
    assert late.alerted_at is None and "nobody was told" in caplog.text


@override_settings(MODERATION_ALERT_EMAILS=["ops@example.org"])
@pytest.mark.django_db
def test_if_the_mail_cannot_be_sent_the_report_is_still_unalerted(monkeypatch):
    class Down:
        def send_notice(self, **k):
            raise RuntimeError("mail provider down")
    monkeypatch.setattr(sweeps, "get_sender", lambda: Down())
    late = _flag(age_hours=30)
    assert sweeps.alert_overdue_flags() == []
    late.refresh_from_db()
    assert late.alerted_at is None


@override_settings(MODERATION_ALERT_EMAILS=["ops@example.org", "lead@example.org"])
@pytest.mark.django_db
def test_the_mail_has_counts_and_ages_and_none_of_the_reporters_words_or_names(monkeypatch):
    sent = []

    class Spy:
        def send_notice(self, *, to, subject, body):
            sent.append((to, subject, body))
    monkeypatch.setattr(sweeps, "get_sender", lambda: Spy())
    reporter = AccountFactory(display_name="Marites Santos")
    _flag(reporter, code="animal_welfare", reason="a secret detail about someone", age_hours=6)
    _flag(reporter, reason="another private thing", age_hours=30)
    sweeps.alert_overdue_flags()
    assert [s[0] for s in sent] == ["ops@example.org", "lead@example.org"]
    to, subject, body = sent[0]
    assert "2 report(s)" in subject and "animal that may be in danger" in body and "oldest 6h" in body and "oldest 30h" in body
    for private in ("Marites", "secret detail", "private thing", reporter.email):
        assert private not in body and private not in subject


@override_settings(MODERATION_ALERT_EMAILS=["ops@example.org"])
@pytest.mark.django_db
def test_the_hourly_sweep_runs_it():
    from sagip.management.commands.run_sweeps import SWEEPS
    assert "flags_overdue" in [label for label, _ in SWEEPS]


# ---- step 24 · the words are not kept forever --------------------------------------------------------------------------
@pytest.mark.django_db
def test_a_dismissed_reports_text_is_scrubbed_after_a_year_but_an_upheld_ones_is_kept():
    old_dismissed = _flag(status=FlagStatus.DISMISSED, reason="private words", decided_days_ago=400)
    old_actioned = _flag(status=FlagStatus.ACTIONED, reason="the welfare record", decided_days_ago=400)
    recent_dismissed = _flag(status=FlagStatus.DISMISSED, reason="recent", decided_days_ago=100)
    still_open = _flag(reason="being worked", age_hours=24 * 500)
    reviewed = _flag(status=FlagStatus.REVIEWED, reason="in review", decided_days_ago=500)

    assert retention.scrub_old_reasons() == 1

    for flag, expected in ((old_dismissed, ""), (old_actioned, "the welfare record"), (recent_dismissed, "recent"),
                           (still_open, "being worked"), (reviewed, "in review")):
        flag.refresh_from_db()
        assert flag.reason == expected
    old_dismissed.refresh_from_db()
    assert old_dismissed.status == FlagStatus.DISMISSED and old_dismissed.reason_code == "harassment"   # the row and category stay


@pytest.mark.django_db
def test_the_snapshot_of_the_targets_text_goes_with_a_dismissed_reports_words():
    """A snapshot is a copy of somebody else's text; it is held no longer than the report's own words are. An upheld one keeps it."""
    snap = {"title": "t", "body": "someone's words", "photos": []}
    gone = _flag(status=FlagStatus.DISMISSED, reason="", decided_days_ago=400)
    kept = _flag(status=FlagStatus.ACTIONED, reason="", decided_days_ago=400)
    ModerationFlag.objects.filter(pk__in=[gone.pk, kept.pk]).update(target_snapshot=snap)
    assert retention.scrub_old_reasons() == 1
    gone.refresh_from_db()
    kept.refresh_from_db()
    assert gone.target_snapshot is None and kept.target_snapshot == snap


@pytest.mark.django_db
def test_scrubbing_is_idempotent_and_the_window_is_a_setting():
    _flag(status=FlagStatus.DISMISSED, reason="words", decided_days_ago=40)
    assert retention.scrub_old_reasons() == 0
    with override_settings(MODERATION_REASON_RETENTION_DAYS=30):
        assert retention.scrub_old_reasons() == 1
        assert retention.scrub_old_reasons() == 0


@pytest.mark.django_db
def test_the_command_scrubs_and_says_how_many(capsys):
    _flag(status=FlagStatus.DISMISSED, reason="words", decided_days_ago=400)
    call_command("scrub_flag_reasons")
    assert "scrubbed 1 report(s)" in capsys.readouterr().out
