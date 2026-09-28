"""P6 · the activity-cancel cascade, written once (shelter cancel, staff suspension, staff
close). One transaction: a half-cancelled activity strands people who think they are booked."""
from django.db import transaction

from notifications.service import notify
from volunteer.models import ShiftStatus, SignupStatus, VolunteerShift
from volunteer.status import set_signup_status

LIVE = (SignupStatus.REQUESTED, SignupStatus.APPROVED)


def cancel_activity(shift, *, by, body):
    with transaction.atomic():
        shift = VolunteerShift.objects.select_for_update().get(pk=shift.pk)
        shift.status = ShiftStatus.CLOSED
        shift.save(update_fields=["status", "updated_at"])
        # of=("self",): lock the signup rows only, not the joined account rows.
        affected = list(shift.signups.select_for_update(of=("self",)).filter(status__in=LIVE)
                        .select_related("volunteer_account"))
        for signup in affected:
            set_signup_status(signup, SignupStatus.CANCELLED, by=by)
            notify(signup.volunteer_account, "shift_cancelled_by_shelter",
                   title="An activity you signed up for was cancelled", body=body,
                   data={"shift_id": str(shift.pk)})
    return len(affected)
