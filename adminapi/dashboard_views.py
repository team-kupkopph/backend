"""US-D2 · the dashboard's counts, in one request."""
from django.utils import timezone
from rest_framework.response import Response

from accounts.models import Account, AccountStatus
from adminapi.verifications_views import StaffView
from moderation.models import FlagStatus, ModerationFlag
from shelter.models import DonationQr
from verifications.models import VerificationStatus, VerificationRequest


class DashboardView(StaffView):
    """GET /admin-api/dashboard

    ⚠️ Counts are EXACT, not estimated. At launch volume there is nothing to optimise, and an
    approximate backlog is worse than a slow one: a reviewer who cannot trust "7 pending" has
    to open the queue to check, which is the work the number was meant to save.

    Every stat is a queue depth, which is why each carries the filter that opens it — a number
    a reviewer cannot click is a number they cannot act on.
    """

    def get(self, request):
        week_ago = timezone.now() - timezone.timedelta(days=7)
        return Response({
            "pending_verifications": {
                "count": VerificationRequest.objects.filter(status=VerificationStatus.PENDING).count(),
                "href": "/verifications?status=pending",
            },
            "open_flags": {
                "count": ModerationFlag.objects.filter(status=FlagStatus.OPEN).count(),
                "href": "/moderation?status=open",
            },
            "unverified_donation_qrs": {
                "count": DonationQr.objects.filter(verified=False).count(),
                "href": "/donations?verified=false",
            },
            "new_members_this_week": {
                "count": Account.objects.filter(created_at__gte=week_ago,
                                                status=AccountStatus.ACTIVE).count(),
                "href": "/members",
            },
        })
