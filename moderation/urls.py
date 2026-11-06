from django.urls import path

from moderation.views import BlockDetailView, BlocksView, ModerationFlagCreateView

urlpatterns = [
    path("moderation/flags", ModerationFlagCreateView.as_view()),
    path("me/blocks", BlocksView.as_view()),
    path("me/blocks/<uuid:block_id>", BlockDetailView.as_view()),
]
