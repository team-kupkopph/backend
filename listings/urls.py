from django.urls import path

from listings.views import (
                            CaseListView,
                            CasePlaceView,
                            InquiryStageView,
                            ListingDetailView,
                            ListingInquiriesView,
                            ListingsView,
                            MyInquiriesView,
                            MyPetsView,
                            PlacementDecisionView,
)

urlpatterns = [
    path("listings", ListingsView.as_view()),
    path("listings/<uuid:listing_id>", ListingDetailView.as_view()),
    path("listings/<uuid:listing_id>/inquiries", ListingInquiriesView.as_view()),
    path("me/inquiries", MyInquiriesView.as_view()),
    path("me/pets", MyPetsView.as_view()),
    path("inquiries/<uuid:inquiry_id>/stages/<str:stage_key>", InquiryStageView.as_view()),
    path("inquiries/<uuid:inquiry_id>/accept", PlacementDecisionView.as_view(), {"action": "accept"}),
    path("inquiries/<uuid:inquiry_id>/decline", PlacementDecisionView.as_view(), {"action": "decline"}),
    path("cases/<uuid:case_id>/list", CaseListView.as_view()),
    path("cases/<uuid:case_id>/place", CasePlaceView.as_view()),
]
