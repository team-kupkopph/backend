"""US-V7 · the volunteer reminder sweep.

Windows are Tech Spec §14's: 24 h and 1 h before the shift. (`dev/volunteer-feature.md`
says only "ahead of the shift" — §14's numbers win; reconcile that doc.)

⚠️ Idempotency is DERIVED, not stored. Cron runs this hourly, so without a guard every
approved volunteer would be reminded every hour. Escalation solved the analogous problem
with a stored `escalation_level`; here the notification rows themselves are the record —
a reminder is due only if no `shift_reminder` row exists for that signup AND window. No
migration, and it handles both windows independently (one boolean could not).
"""
from django.utils import timezone

from notifications.models import Notification
from notifications.service import notify

from .models import SignupStatus, VolunteerSignup

# (label, upper_hours, lower_hours): remind when lower_h < (starts_at - now) <= upper_h.
# Bands are disjoint so a shift within 1h is ONLY in the 1h band — it never also gets the
# (wrong) 24h message. A shift that slips straight past the 24h band (cron downtime,
# late walk-in approval) correctly gets only the 1h reminder, not a false "24h from now".
REMINDER_WINDOWS = [("24h", 24, 1), ("1h", 1, 0)]


def _clock(dt):
    local = timezone.localtime(dt)
    return f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"


def _reminder_body(signup, label, now):
    shift = signup.shift
    what = f"{shift.title or shift.get_type_display()} at {shift.shelter_account.display_name}"
    if label == "1h":
        return f"{what} starts at {_clock(shift.starts_at)}. See you there!"
    same_day = timezone.localtime(shift.starts_at).date() == timezone.localtime(now).date()
    return f"{what} starts {'today' if same_day else 'tomorrow'} at {_clock(shift.starts_at)}."


def remind_shifts(now=None):
    """Send any due shift reminders. Idempotent. Returns the signups reminded."""
    now = now or timezone.now()
    reminded = []
    for label, upper_h, lower_h in REMINDER_WINDOWS:
        lower = now + timezone.timedelta(hours=lower_h)
        upper = now + timezone.timedelta(hours=upper_h)
        due = (VolunteerSignup.objects
               .filter(status=SignupStatus.APPROVED,
                       shift__starts_at__gt=lower,
                       shift__starts_at__lte=upper)
               .select_related("shift", "volunteer_account"))
        for signup in due:
            already = Notification.objects.filter(
                account=signup.volunteer_account, type="shift_reminder",
                data__signup_id=str(signup.pk), data__window=label).exists()
            if already:
                continue
            notify(signup.volunteer_account, "shift_reminder",
                   title="Your shift is coming up",
                   body=_reminder_body(signup, label, now),
                   data={"shift_id": str(signup.shift_id), "signup_id": str(signup.pk),
                         "window": label})
            reminded.append(signup)
    return reminded
