from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView


def _repr(n):
    return {"notification_id": str(n.notification_id), "type": n.type,
            "title": n.title or None, "body": n.body or None, "data": n.data,
            "read": n.read, "created_at": n.created_at.isoformat()}


class NotificationsView(APIView):
    """US-X1 · the bell. The caller's own notifications, newest first."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = request.user.notifications.order_by("-created_at")
        return Response({"notifications": [_repr(n) for n in qs]})


class NotificationsUnreadCountView(APIView):
    """SH12 (shelter home review) · GET /me/notifications/unread-count — the bell's dot. Each Home used to fetch the
    whole list on every focus only to ask "is anything unread?"."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"unread": request.user.notifications.filter(read=False).count()})


class NotificationsReadView(APIView):
    """Mark the caller's unread notifications read (on opening the bell)."""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        marked = request.user.notifications.filter(read=False).update(read=True)
        return Response({"marked_read": marked})
