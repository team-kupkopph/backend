from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from devices.models import DeviceToken

_PLATFORMS = {"ios", "android"}

class DeviceTokensView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        fcm = (request.data.get("fcm_token") or "").strip()
        platform = request.data.get("platform")
        if not fcm or platform not in _PLATFORMS:
            return Response({"error": {"code": "bad_request",
                                       "message": "fcm_token and platform (ios|android) required"}},
                            status=422)
        # Upsert on the unique token — re-home it to the caller (shared-device fix).
        tok, _ = DeviceToken.objects.update_or_create(
            fcm_token=fcm,
            defaults={"account": request.user, "platform": platform,
                      "last_used_at": timezone.now()})
        return Response({"token_id": str(tok.token_id)}, status=201)


class DeviceTokenDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, token_id):
        deleted, _ = DeviceToken.objects.filter(pk=token_id, account=request.user).delete()
        if not deleted:
            return Response({"error": {"code": "not_found", "message": "No such token"}}, status=404)
        return Response(status=204)
