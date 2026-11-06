from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.throttles import ModerationFlagCreateThrottle
from moderation.models import FlagStatus, ModerationFlag
from moderation.serializers import ModerationFlagCreateSerializer
from moderation.targets import target_exists


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
