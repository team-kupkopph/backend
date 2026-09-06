from django.urls import path

from volunteer.views import (
                             MySignupsView,
                             ShelterShiftCancelView,
                             ShelterShiftDetailView,
                             ShelterShiftRosterView,
                             ShelterShiftsView,
                             ShelterSignupVolunteerView,
                             ShiftDetailView,
                             ShiftRequestsView,
                             ShiftsBrowseView,
                             ShiftSignupView,
                             SignupApproveView,
                             SignupAttendanceView,
                             SignupCancelView,
                             SignupCheckView,
                             SignupDeclineView,
)

urlpatterns = [
    path("me/signups", MySignupsView.as_view()),
    path("shelter/shifts", ShelterShiftsView.as_view()),
    path("shelter/shifts/<uuid:shift_id>", ShelterShiftDetailView.as_view()),
    path("shelter/shifts/<uuid:shift_id>/cancel", ShelterShiftCancelView.as_view()),
    path("shelter/shifts/<uuid:shift_id>/requests", ShiftRequestsView.as_view()),
    path("shelter/shifts/<uuid:shift_id>/roster", ShelterShiftRosterView.as_view()),
    path("shelter/signups/<uuid:signup_id>/approve", SignupApproveView.as_view()),
    path("shelter/signups/<uuid:signup_id>/volunteer", ShelterSignupVolunteerView.as_view()),
    path("shelter/signups/<uuid:signup_id>/decline", SignupDeclineView.as_view()),
    path("shelter/signups/<uuid:signup_id>/attendance", SignupAttendanceView.as_view()),
    path("shifts", ShiftsBrowseView.as_view()),
    path("shifts/<uuid:shift_id>", ShiftDetailView.as_view()),
    path("shifts/<uuid:shift_id>/signups", ShiftSignupView.as_view()),
    path("signups/<uuid:signup_id>/cancel", SignupCancelView.as_view()),
    path("signups/<uuid:signup_id>/check-in", SignupCheckView.as_view(), {"action": "in"}),
    path("signups/<uuid:signup_id>/check-out", SignupCheckView.as_view(), {"action": "out"}),
]
