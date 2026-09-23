import pytest
from django.core.management import call_command
from django.utils import timezone

from accounts.factories import AccountFactory
from notifications.models import Notification
from volunteer.models import ShiftStatus, SignupStatus, VolunteerShift, VolunteerSignup
from volunteer.sweeps import remind_shifts


def _approved(hours_out):
    shift = VolunteerShift.objects.create(
        shelter_account=AccountFactory(account_type="shelter"),
        starts_at=timezone.now() + timezone.timedelta(hours=hours_out),
        ends_at=timezone.now() + timezone.timedelta(hours=hours_out + 2),
        capacity=2, status=ShiftStatus.OPEN)
    return VolunteerSignup.objects.create(shift=shift, volunteer_account=AccountFactory(),
                                          status=SignupStatus.APPROVED, waiver_accepted=True)


def _reminders(su, window=None):
    qs = Notification.objects.filter(account=su.volunteer_account, type="shift_reminder")
    if window:
        qs = qs.filter(data__window=window)
    return qs.count()


@pytest.mark.django_db
def test_a_shift_inside_the_24h_window_is_reminded():
    su = _approved(hours_out=20)
    remind_shifts()
    assert _reminders(su, "24h") == 1


@pytest.mark.django_db
def test_a_distant_shift_is_not_reminded_yet():
    su = _approved(hours_out=72)
    remind_shifts()
    assert _reminders(su) == 0


@pytest.mark.django_db
def test_the_sweep_is_idempotent_across_repeated_runs():
    """The whole point: cron runs hourly and must not re-send every hour."""
    su = _approved(hours_out=20)
    for _ in range(4):
        remind_shifts()
    assert _reminders(su, "24h") == 1


@pytest.mark.django_db
def test_the_two_windows_are_independent():
    su = _approved(hours_out=20)
    remind_shifts()
    assert _reminders(su, "24h") == 1 and _reminders(su, "1h") == 0
    # time passes: the shift is now within the hour
    VolunteerShift.objects.filter(pk=su.shift_id).update(
        starts_at=timezone.now() + timezone.timedelta(minutes=40))
    remind_shifts()
    assert _reminders(su, "24h") == 1, "the 24h reminder must not repeat"
    assert _reminders(su, "1h") == 1


@pytest.mark.django_db
def test_only_approved_signups_are_reminded():
    su = _approved(hours_out=20)
    VolunteerSignup.objects.filter(pk=su.pk).update(status=SignupStatus.CANCELLED)
    remind_shifts()
    assert _reminders(su) == 0


@pytest.mark.django_db
def test_a_past_shift_is_not_reminded():
    su = _approved(hours_out=-5)
    remind_shifts()
    assert _reminders(su) == 0


@pytest.mark.django_db
def test_a_shift_first_seen_inside_the_1h_window_gets_only_the_1h_reminder():
    """A shift ~30 min out, never swept before, falls inside BOTH the (0,1h] and (0,24h]
    ranges if those ranges are nested — producing a spurious 'starts 24 hours from now'
    notification alongside the correct 1h one. Bands must be disjoint so this signup is
    matched by the 1h band only."""
    shift = VolunteerShift.objects.create(
        shelter_account=AccountFactory(account_type="shelter"),
        starts_at=timezone.now() + timezone.timedelta(minutes=30),
        ends_at=timezone.now() + timezone.timedelta(minutes=30, hours=2),
        capacity=2, status=ShiftStatus.OPEN)
    su = VolunteerSignup.objects.create(shift=shift, volunteer_account=AccountFactory(),
                                        status=SignupStatus.APPROVED, waiver_accepted=True)
    remind_shifts()
    assert _reminders(su) == 1
    assert _reminders(su, "1h") == 1
    assert _reminders(su, "24h") == 0


@pytest.mark.django_db
def test_run_sweeps_reports_the_volunteer_reminders(capsys):
    _approved(hours_out=20)
    call_command("run_sweeps")
    out = capsys.readouterr().out
    assert "reminded" in out


@pytest.mark.django_db
def test_reminder_copy_names_the_local_time_and_day():
    from zoneinfo import ZoneInfo
    manila = ZoneInfo("Asia/Manila")
    now = timezone.datetime(2030, 10, 3, 10, 0, tzinfo=manila)
    start = timezone.datetime(2030, 10, 4, 9, 0, tzinfo=manila)          # 23 h later
    shift = VolunteerShift.objects.create(shelter_account=AccountFactory(account_type="shelter",
                                          display_name="KG Test Shelter"),
                                          starts_at=start, ends_at=start + timezone.timedelta(hours=2),
                                          capacity=2, title="Morning dog walk")
    VolunteerSignup.objects.create(shift=shift, volunteer_account=AccountFactory(),
                                   status=SignupStatus.APPROVED)
    remind_shifts(now=now)
    n = Notification.objects.get(type="shift_reminder")
    assert n.body == "Morning dog walk at KG Test Shelter starts tomorrow at 9:00 AM."
