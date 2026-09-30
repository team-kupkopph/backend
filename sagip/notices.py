"""What the rescue loop tells people, and when (dev/sagip-build-review.md S9 · S10 · S18).

One place for the wording, so the status screen, the sweeps and the placement decision can't
drift into three voices for the same event. Every function here only calls `notify()`; none
of them decides whether something happened — the caller already did.
"""
from notifications.service import notify
from sagip.models import OfferStatus, StrayStatus

# S10 · what the reporter is told at each step. The claimer is named: the reporter already
# learned it from `report_claimed`, and a name is what turns "status: safe" into news.
_PROGRESS = {
    StrayStatus.RESCUED: ("Rescued", "{claimer} picked up the {species} you reported."),
    StrayStatus.SAFE: ("Safe now", "The {species} you reported is safe with {claimer}."),
    StrayStatus.RESOLVED: ("Rescue complete", "The rescue of the {species} you reported is done. "
                                              "See how it ended."),
}


def _species(report):
    return report.get_species_display().lower()


def case_progress(report, case, status):
    """S10 · tell the reporter the case moved, and — only when it ENDS — the offerers whose
    help was matched to it. Offerers hear the ending, not every step: they offered transport,
    not a subscription."""
    title, body = _PROGRESS[status]
    data = {"report_id": str(report.pk), "case_id": str(case.pk), "status": status}
    if report.reporter_account_id:
        notify(report.reporter_account, "case_progress", title=title,
               body=body.format(claimer=case.claimed_by_account.display_name,
                                species=_species(report)),
               data=data)
    if status == StrayStatus.RESOLVED:
        for offer in report.offers.filter(status=OfferStatus.MATCHED).select_related("account"):
            notify(offer.account, "case_progress", title="A rescue you helped is complete",
                   body=f"The {_species(report)} you offered to help is safe.", data=data)


def claim_due(case, hours_left):
    """S9 · the claimer is warned while there is still time to act."""
    report = case.report
    notify(case.claimed_by_account, "claim_due", title="Your claim needs an update",
           body=f"Post an update on the {_species(report)} within about {hours_left} h, "
                f"or it reopens for another rescuer.",
           data={"report_id": str(report.pk), "case_id": str(case.pk)})


def claim_lapsed(case, window_hours):
    """S9 · the claimer and the reporter are told the claim was taken back. The claim
    confirm said a claim "can't be handed back"; the sweep then did it silently."""
    report = case.report
    where = report.city or "the area"
    notify(case.claimed_by_account, "claim_lapsed", title="Your claim lapsed",
           body=f"No update came within {window_hours} h, so the {_species(report)} in {where} "
                f"is back on the map for another rescuer.",
           data={"report_id": str(report.pk), "case_id": str(case.pk)})
    if report.reporter_account_id:
        notify(report.reporter_account, "case_reopened", title="Your report is open again",
               body="The rescuer's claim lapsed, so your report is back on the map for others.",
               data={"report_id": str(report.pk)})


def placement_decided(listing, inquiry, decision):
    """S18 · the rescuer hears the recipient's answer either way."""
    name = listing.name or "the animal"
    who = inquiry.adopter_account.display_name
    if decision == "accepted":
        title, body = "Placement accepted", f"{who} accepted {name}. The rescue is complete."
    else:
        title, body = ("Placement declined",
                       f"{who} declined {name}. You can place them with someone else or list "
                       f"them for adoption.")
    notify(listing.posted_by, "placement_decided", title=title, body=body,
           data={"listing_id": str(listing.pk), "inquiry_id": str(inquiry.pk),
                 "decision": decision})


# D3 · what the reporter is told when the claimer releases. Keyed by
# serializers.ClaimReleaseSerializer's reasons.
RELEASE_REASON_TEXT = {
    "cant_get_there": "they can't get there",
    "cant_find": "they couldn't find the animal",
    "no_capacity": "they can't take the animal in",
    "something_came_up": "something came up",
}


def claim_released(case, reason):
    """D3 · the reporter hears at once that the rescuer couldn't make it and the report is open
    again — the release exists so nobody waits out a claim window for a claimer who knows
    they aren't coming. (Offerers are told by sweeps.reopen_case.)"""
    report = case.report
    if not report.reporter_account_id:
        return
    why = RELEASE_REASON_TEXT.get(reason, "something came up")
    notify(report.reporter_account, "case_reopened", title="Your report is open again",
           body=f"The rescuer couldn't make it ({why}), so your report is back on the map "
                f"for someone else to claim.",
           data={"report_id": str(report.pk)})
