from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Account
from common.throttles import BlockCreateThrottle, ModerationFlagCreateThrottle
from moderation.models import AccountBlock, FlagStatus, FlagTarget, ModerationFlag
from moderation.serializers import BlockCreateSerializer, ModerationFlagCreateSerializer
from moderation.targets import resolve_person, target_exists


def _own_account():
    return Response({"error": {"code": "own_account", "message": "That's you."}}, status=422)


class ModerationFlagCreateView(APIView):
    """US-M1 · "Report this" on a stray report or listing. Duplicate flags by the same
    account on the same target COLLAPSE — a second tap while the first flag is still
    `open` returns the existing flag rather than creating a second row; the target is
    already in the queue either way. A target flagged again after its earlier flag was
    resolved (reviewed/actioned/dismissed) opens a fresh one — the earlier decision
    doesn't pre-empt a genuinely new concern.

    Review step 7: a report carries a category, and a PERSON can be reported, by account id or by what they posted."""
    permission_classes = [IsAuthenticated]
    throttle_classes = [ModerationFlagCreateThrottle]  # US-SEC2 pattern · per-account, 20/day

    def post(self, request):
        s = ModerationFlagCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        if d.get("via_type"):
            person = resolve_person(d["via_type"], d["via_id"])
            if person is None:
                return Response({"error": {"code": "not_found", "message": "That item no longer exists."}}, status=404)
            target_id = person.pk
        else:
            target_id = d["target_id"]
        if d["target_type"] == FlagTarget.ACCOUNT and target_id == request.user.pk:
            return _own_account()
        if not target_exists(d["target_type"], target_id):
            return Response({"error": {"code": "not_found", "message": "That item no longer exists."}}, status=404)

        existing = ModerationFlag.objects.filter(
            reporter_account=request.user, target_type=d["target_type"],
            target_id=target_id, status=FlagStatus.OPEN).first()
        if existing is not None:
            return Response({"flag_id": str(existing.pk)}, status=200)

        flag = ModerationFlag.objects.create(
            reporter_account=request.user, target_type=d["target_type"], target_id=target_id,
            reason=d["reason"], reason_code=d["reason_code"])
        return Response({"flag_id": str(flag.pk)}, status=201)


class BlocksView(APIView):
    """Review step 7d · `GET /me/blocks` is the people I have blocked; `POST /me/blocks` blocks one.

    The account id of a blocked person is never handed back: only their name (which the blocker has already seen) and
    the id of the block, which is what Unblock takes."""
    permission_classes = [IsAuthenticated]

    def get_throttles(self):
        return [BlockCreateThrottle()] if self.request.method == "POST" else []

    def get(self, request):
        rows = AccountBlock.objects.filter(blocker=request.user).select_related("blocked").order_by("-created_at")
        return Response({"results": [{"block_id": str(b.pk), "display_name": b.blocked.display_name,
                                      "blocked_at": b.created_at.isoformat()} for b in rows]})

    def post(self, request):
        s = BlockCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        person = (resolve_person(d["via_type"], d["via_id"]) if d.get("via_type")
                  else Account.objects.filter(pk=d["account_id"]).first())
        if person is None:
            return Response({"error": {"code": "not_found", "message": "That person is no longer here."}}, status=404)
        if person.pk == request.user.pk:
            return _own_account()
        block, created = AccountBlock.objects.get_or_create(blocker=request.user, blocked=person)
        return Response({"block_id": str(block.pk), "display_name": person.display_name},
                        status=201 if created else 200)


class BlockDetailView(APIView):
    """`DELETE /me/blocks/{block_id}` · Unblock. Only your own block; anyone else's is a 404."""
    permission_classes = [IsAuthenticated]

    def delete(self, request, block_id):
        deleted, _ = AccountBlock.objects.filter(pk=block_id, blocker=request.user).delete()
        if not deleted:
            return Response({"error": {"code": "not_found", "message": "No such block."}}, status=404)
        return Response(status=204)
