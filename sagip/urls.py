from django.urls import path

from sagip.views import (
                         CaseDetailView,
                         CaseStatusView,
                         MyOffersView,
                         MyReportsView,
                         MyRescuesView,
                         ReportClaimView,
                         ReportDetailView,
                         ReportMatchDecisionView,
                         ReportMatchesView,
                         ReportOffersView,
                         ReportOfferWithdrawView,
                         ReportsCreateView,
                         RescueMapView,
)

urlpatterns = [
    path("reports", ReportsCreateView.as_view()),
    path("reports/map", RescueMapView.as_view()),
    path("reports/<uuid:report_id>", ReportDetailView.as_view()),
    path("reports/<uuid:report_id>/claim", ReportClaimView.as_view()),
    path("reports/<uuid:report_id>/matches", ReportMatchesView.as_view()),
    path("reports/<uuid:report_id>/matches/<uuid:match_id>/confirm",
         ReportMatchDecisionView.as_view(), {"action": "confirm"}),
    path("reports/<uuid:report_id>/matches/<uuid:match_id>/dismiss",
         ReportMatchDecisionView.as_view(), {"action": "dismiss"}),
    path("reports/<uuid:report_id>/offers", ReportOffersView.as_view()),
    path("reports/<uuid:report_id>/offers/<uuid:offer_id>", ReportOfferWithdrawView.as_view()),
    path("cases/<uuid:case_id>", CaseDetailView.as_view()),
    path("cases/<uuid:case_id>/status", CaseStatusView.as_view()),
    path("me/reports", MyReportsView.as_view()),
    path("me/rescues", MyRescuesView.as_view()),
    path("me/offers", MyOffersView.as_view()),
]
