from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.throttles import ModerationFlagCreateThrottle
from moderation.models import FlagStatus, FlagTarget, ModerationFlag
from moderation.serializers import ModerationFlagCreateSerializer


def target_exists(target_type, target_id):
    """AT28 · a flag must name a row that exists as the type it claims, or flags on nothing pile up in the moderators' queue. `message` has no
    model in this codebase, so it cannot be checked and is accepted as before."""
    from accounts.models import Account
    from community.models import StoryPost
    from listings.models import AdoptionListing
    from sagip.models import StrayReport
    from shelter.models import DonationQr
    from volunteer.models import VolunteerShift

    model = {
        FlagTarget.ACCOUNT: (Account, "account_id"), FlagTarget.LISTING: (AdoptionListing, "listing_id"),
        FlagTarget.REPORT: (StrayReport, "report_id"), FlagTarget.QR: (DonationQr, "donation_qr_id"),
        FlagTarget.STORY: (StoryPost, "story_id"), FlagTarget.SHIFT: (VolunteerShift, "shift_id"),
    }.get(target_type)
    if model is None:
        return True
    cls, pk = model
    return cls.objects.filter(**{pk: target_id}).exists()


class ModerationFlagCreateView(APIView):
    """US-M1 · "Report this" on a stray report or listing. Duplicate flags by the same
    account on the same target COLLAPSE — a second tap while the first flag is still
    `open` returns the existing flag rather than creating a second row; the target is
    already in the queue either way. A target flagged again after its earlier flag was
    resolved (reviewed/actioned/dismissed) opens a fresh one — the earlier decision
    doesn't pre-empt a genuinely new concern."""
    permission_classes = [IsAuthenticated]
    throttle_classes = [ModerationFlagCreateThrottle]  # US-SEC2 pattern · per-account, 20/day

    def post(self, request):
        s = ModerationFlagCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        if not target_exists(d["target_type"], d["target_id"]):
            return Response({"error": {"code": "not_found", "message": "That item no longer exists."}}, status=404)

        existing = ModerationFlag.objects.filter(
            reporter_account=request.user, target_type=d["target_type"],
            target_id=d["target_id"], status=FlagStatus.OPEN).first()
        if existing is not None:
            return Response({"flag_id": str(existing.pk)}, status=200)

        flag = ModerationFlag.objects.create(
            reporter_account=request.user, target_type=d["target_type"],
            target_id=d["target_id"], reason=d["reason"])
        return Response({"flag_id": str(flag.pk)}, status=201)
