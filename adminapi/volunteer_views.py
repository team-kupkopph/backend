from rest_framework.response import Response

from adminapi.verifications_views import StaffView
from volunteer.models import ShiftStatus, VolunteerShift
from volunteer.services import cancel_activity


class ShiftCloseView(StaffView):
    """P6 · G21. POST /admin-api/shifts/{id}/close {reason} — staff take a bad activity down.
    Uses the same cascade as the shelter's own cancel; volunteers are told it's no longer
    running, never why."""

    def post(self, request, shift_id):
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            return Response({"error": {"code": "reason_required"}}, status=422)
        shift = VolunteerShift.objects.filter(pk=shift_id).first()
        if shift is None:
            return Response({"error": {"code": "not_found"}}, status=404)
        if shift.status == ShiftStatus.CLOSED:
            return Response({"error": {"code": "shift_closed"}}, status=409)
        count = cancel_activity(shift, by="platform", body="This activity is no longer running.")
        request._audit_body = {"reason": reason}
        return Response({"cancelled_signups": count})
