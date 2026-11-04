from django.urls import path

from notifications.views import NotificationsReadView, NotificationsUnreadCountView, NotificationsView

urlpatterns = [
    path("me/notifications", NotificationsView.as_view()),
    path("me/notifications/read", NotificationsReadView.as_view()),
    path("me/notifications/unread-count", NotificationsUnreadCountView.as_view()),   # SH12
]
