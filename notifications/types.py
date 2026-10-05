"""US-N1 · the notification-type registry.

Before this existed, `type` was free-text at every `notify()` call site with no shared
source of truth — and building `NotificationsScreen` in Sprint 3 required hand-maintaining
a *second*, mobile-side list (`mobile_app/src/notifications.ts::notificationTarget()`),
kept in sync with these call sites by hand because there was nothing to import. This is
the backend half of closing that gap: every type any `notify()` call site actually uses,
recorded once, with the `data` shape a client needs to deep-link a tap. `notify()` (see
`notifications/service.py`) refuses an unregistered type outright, so a typo'd or
newly-invented type can't silently ship a notification nothing knows how to route.

This is also the input Sprint 5's push matrix (§14) consumes — build the registry before
the delivery channel multiplies types further.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class NotificationType:
    key: str
    # Documents what `data` carries, for readers (this repo's and the mobile client's) —
    # not runtime-validated against the dict a call site actually passes; each call site
    # already builds `data` at the point that has the real values.
    data_shape: str
    push: bool
    deep_link: str  # a kupkop:// template keyed on `data`, or "" for no target


_TYPES = [
    NotificationType("verification_approved", "{verification_id, type}", True, "kupkop://verifications"),
    NotificationType("verification_rejected", "{verification_id, type}", True, "kupkop://verifications"),
    NotificationType("verification_needs_info", "{verification_id, type}", True, "kupkop://verifications"),
    NotificationType("report_escalated", "{report_id, escalation_level}", True, "kupkop://reports/{report_id}"),
    NotificationType("case_reopened", "{report_id}", True, "kupkop://reports/{report_id}"),
    NotificationType("offer_matched", "{report_id, case_id}", True, "kupkop://reports/{report_id}"),
    NotificationType("report_claimed", "{report_id, case_id}", True, "kupkop://reports/{report_id}"),
    NotificationType("offer_received", "{report_id, offer_id}", True, "kupkop://reports/{report_id}"),
    NotificationType("inquiry_received", "{listing_id, inquiry_id, poster_is_shelter}", True,
                     "kupkop://inquiries/{inquiry_id}"),
    NotificationType("stage_advanced", "{inquiry_id, stage_key}", True, "kupkop://inquiries"),
    NotificationType("signup_requested", "{shift_id, signup_id}", True, "kupkop://shelter/shifts/{shift_id}/requests"),
    NotificationType("shift_confirmed", "{shift_id, signup_id}", True, "kupkop://shifts"),
    NotificationType("signup_declined", "{shift_id, signup_id}", True, "kupkop://shifts/history"),
    NotificationType("shift_cancelled_by_shelter", "{shift_id}", True, "kupkop://shifts/history"),
    NotificationType("shift_reminder", "{shift_id, signup_id, window}", True, "kupkop://shifts"),
    NotificationType("signup_cancelled_by_volunteer", "{shift_id, signup_id, was_late}", True,
                     "kupkop://shelter/shifts/{shift_id}"),
    NotificationType("pledge_received", "{need_id, pledge_id}", True, "kupkop://shelter/needs/{need_id}/pledges"),
    NotificationType("pledge_confirmed", "{need_id, pledge_id}", True, "kupkop://donations"),
    # D-S6-5: a badge is a celebration, in-app only — push:false, so it never buzzes a phone
    # (pushing badges would cheapen push for a match or a pledge).
    NotificationType("badge_earned", "{badge_code}", False, "kupkop://impact"),
    # §11.3: a strong lost<->found suggestion pushes both reporters — a reunion is time-sensitive.
    NotificationType("match_suggested", "{report_id}", True, "kupkop://reports/{report_id}"),
    # P4 · G11/K16.
    NotificationType("signup_cancelled_by_shelter", "{shift_id, signup_id}", True, "kupkop://shifts"),
    NotificationType("attendance_due", "{shift_id, window}", True, "kupkop://shelter/shifts/{shift_id}"),
    # Sagip loop closure (dev/sagip-build-review.md). S10: the reporter (every step) and the
    # matched offerers (the ending only) hear how the rescue went.
    NotificationType("case_progress", "{report_id, case_id, status}", True, "kupkop://reports/{report_id}"),
    # S9: the claimer is warned before a claim lapses, and told when it has.
    NotificationType("claim_due", "{report_id, case_id}", True, "kupkop://cases/{case_id}"),
    NotificationType("claim_lapsed", "{report_id, case_id}", True, "kupkop://reports/{report_id}"),
    NotificationType("report_removed", "{report_id}", True, "kupkop://rescues"),
    # D2 / S6: an urgent report pages verified rescuers + shelters in its city at once
    # (sagip/alerts.py), capped at 5 per person per 24 h.
    NotificationType("report_nearby", "{report_id}", True, "kupkop://reports/{report_id}"),
    # S18: the rescuer hears whether a direct placement was accepted.
    NotificationType("placement_decided", "{listing_id, inquiry_id, decision}", True, "kupkop://rescues"),
    NotificationType("placement_withdrawn", "{listing_id, inquiry_id}", True, "kupkop://inquiries"),
    NotificationType("listing_withdrawn", "{listing_id, inquiry_id}", True, "kupkop://inquiries"),
    # Adoption poster loop (dev/adoption-build-review.md AD2): the placement recipient's push,
    # split from the poster's inquiry_received so the app routes each without guessing.
    NotificationType("placement_offered", "{listing_id, inquiry_id}", True, "kupkop://inquiries"),
    NotificationType("inquiry_accepted", "{listing_id, inquiry_id}", True, "kupkop://inquiries/{inquiry_id}"),
    NotificationType("inquiry_rejected", "{listing_id, inquiry_id}", True, "kupkop://inquiries/{inquiry_id}"),
    NotificationType("inquiry_withdrawn", "{listing_id, inquiry_id, poster_is_shelter}", True, "kupkop://inquiries/{inquiry_id}"),
    NotificationType("adoption_badge_needed", "{listing_id, inquiry_id}", True, "kupkop://inquiries/{inquiry_id}"),
    NotificationType("adoption_reserved", "{listing_id, inquiry_id}", True, "kupkop://inquiries/{inquiry_id}"),
    NotificationType("reservation_released", "{listing_id, inquiry_id}", True, "kupkop://inquiries/{inquiry_id}"),
]

REGISTRY = {t.key: t for t in _TYPES}


def is_registered(type_key):
    return type_key in REGISTRY
