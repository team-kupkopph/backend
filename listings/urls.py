from django.urls import path

from listings.adoption_views import RejectView, ScreenView, WithdrawView
from listings.views import (
    CaseHandoffCancelView,
    CaseListView,
    CasePlaceView,
    InquiryDetailView,
    InquiryStageView,
    ListingDetailView,
    ListingInquiriesView,
    ListingPublishView,
    ListingsView,
    MyInquiriesView,
    MyPetsView,
    PlacementDecisionView,
    ShortlistItemView,
    ShortlistView,
)

urlpatterns = [
    path("listings", ListingsView.as_view()),
    path("listings/<uuid:listing_id>", ListingDetailView.as_view()),
    path("listings/<uuid:listing_id>/publish", ListingPublishView.as_view()),   # D7
    path("listings/<uuid:listing_id>/inquiries", ListingInquiriesView.as_view()),
    path("me/inquiries", MyInquiriesView.as_view()),
    path("me/pets", MyPetsView.as_view()),
    path("me/shortlist", ShortlistView.as_view()),
    path("me/shortlist/<uuid:listing_id>", ShortlistItemView.as_view()),
    path("inquiries/<uuid:inquiry_id>", InquiryDetailView.as_view()),
    path("inquiries/<uuid:inquiry_id>/stages/<str:stage_key>", InquiryStageView.as_view()),
    path("inquiries/<uuid:inquiry_id>/screen", ScreenView.as_view()),               # AQ1
    path("inquiries/<uuid:inquiry_id>/reject", RejectView.as_view()),               # AD6
    path("inquiries/<uuid:inquiry_id>/withdraw", WithdrawView.as_view()),           # AD6
    path("inquiries/<uuid:inquiry_id>/accept", PlacementDecisionView.as_view(), {"action": "accept"}),
    path("inquiries/<uuid:inquiry_id>/decline", PlacementDecisionView.as_view(), {"action": "decline"}),
    path("cases/<uuid:case_id>/list", CaseListView.as_view()),
    path("cases/<uuid:case_id>/place", CasePlaceView.as_view()),
    path("cases/<uuid:case_id>/handoff/cancel", CaseHandoffCancelView.as_view()),
]
