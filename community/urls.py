from django.urls import path

from community.views import (
                             MeImpactView,
                             MyPledgesView,
                             NeedDetailView,
                             NeedPledgesView,
                             NeedReceivedView,
                             PledgeCancelView,
                             ShelterNeedsView,
                             StoriesView,
                             StoryDetailView,
                             StoryReactionView,
)

urlpatterns = [
    path("me/impact", MeImpactView.as_view()),
    path("me/pledges", MyPledgesView.as_view()),
    path("shelters/<uuid:account_id>/needs", ShelterNeedsView.as_view()),
    path("needs/<uuid:need_id>", NeedDetailView.as_view()),
    path("needs/<uuid:need_id>/pledges", NeedPledgesView.as_view()),
    path("needs/<uuid:need_id>/received", NeedReceivedView.as_view()),
    path("pledges/<uuid:pledge_id>/cancel", PledgeCancelView.as_view()),
    path("stories", StoriesView.as_view()),
    path("stories/<uuid:story_id>", StoryDetailView.as_view()),
    path("stories/<uuid:story_id>/reactions", StoryReactionView.as_view()),
]
