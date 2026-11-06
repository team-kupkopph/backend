"""What each flaggable type is, how to read it, and what a takedown does to it (review step 1).

⚠️ WHY THIS FILE EXISTS. `target_type`/`target_id` is a generic pointer with no foreign key, so every
reader has to dispatch on the type. That dispatch lived in four places (the create view's existence
check, the console's describer, `resolve_flag`'s `if` chain, and the console's own label maps), and they
drifted: the describer read `title` off a listing that has `name`, and its `getattr` default turned the
typo into a UUID that looked like content. One spec per type, read by all of them, removes the drift.
`test_every_flag_target_has_a_spec` fails when a `FlagTarget` value is added without one.

Models are named as "app.Model" and resolved on use, so this module imports nothing from the apps it
describes (moderation sits below all of them).
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass

from django.apps import apps

from moderation.models import FlagTarget

logger = logging.getLogger("kupkop.moderation")

TITLE_MAX = 200
BODY_MAX = 2000
PHOTOS_MAX = 10
DEGRADED = "This item couldn't be read."


def _text(value, limit):
    return (value or "").strip()[:limit]


def _first_line(value, limit=80):
    line = (value or "").strip().splitlines()
    line = line[0].strip() if line else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _owner(account):
    if account is None:
        return None
    return {"account_id": str(account.account_id), "display_name": account.display_name}


def _urls(photos):
    return list(photos.values_list("url", flat=True)[:PHOTOS_MAX])


def _view(title, blank, body="", photos=(), owner=None, hidden=None):
    """The one shape every describer returns. `title` is never empty: a blank row reads as `blank`."""
    return {"title": _text(title, TITLE_MAX) or blank, "body": _text(body, BODY_MAX),
            "photos": list(photos), "owner": owner, "hidden": hidden}


def _describe_account(a):
    return _view(a.display_name or a.email, "Account with no name")


def _describe_listing(listing):
    return _view(listing.name, "Listing with no name", listing.story, _urls(listing.photos),
                 _owner(listing.posted_by), listing.hidden_at is not None)


def _describe_report(r):
    return _view(_first_line(r.notes), "Report with no notes", r.notes, _urls(r.photos),
                 _owner(r.reporter_account), r.hidden_at is not None)


def _describe_qr(q):
    return _view(f"{q.get_provider_display()} · {q.account_name}", "Donation QR with no payee",
                 photos=[q.qr_image_url] if q.qr_image_url else [], owner=_owner(q.account))


def _describe_story(s):
    from community.models import StoryStatus
    return _view(_first_line(s.caption), "Story with no caption", s.caption, _urls(s.photos),
                 _owner(s.author_account), s.status == StoryStatus.HIDDEN)


def _describe_shift(s):
    return _view(s.title, "Shift with no title", s.description, owner=_owner(s.shelter_account))


# ---- takedowns. Each returns True only when it changed something, so `side_effects` never claims
# work that did not happen (MO17: a second flag on an already hidden listing used to say it hid it).
def _hide_story(story, reviewer):
    from community.models import StoryPost, StoryStatus
    # US-T3 / D-S6-4 · hiding, NOT deletion. The row and its photos survive, the feed excludes it, the
    # author sees a "hidden by moderation" state rather than a vanished post. The flag stays as the audit trail.
    return bool(StoryPost.objects.filter(pk=story.pk).exclude(status=StoryStatus.HIDDEN)
                .update(status=StoryStatus.HIDDEN))


def _hide_report(report, reviewer):
    from sagip.moderation import hide_report  # C13 · hide, never delete
    return hide_report(report, reviewer)


def _hide_listing(listing, reviewer):
    from listings.moderation import hide_listing  # AD7 / AQ7 · hide, never delete
    return hide_listing(listing, reviewer)


# ---- what actioning will do, in plain words, BEFORE the click (review step 3, MO3). Each function reads
# the same rows the take-down above is about to change, with the same filters, and
# `test_on_action.py` holds the stated numbers to what the take-down then really changes: a warning
# that undercounts is worse than none. Keep a function and its take-down in step.
def _n(count, one, many):
    return f"{count} {one if count == 1 else many}"


def _listing_consequences(listing):
    from listings.models import AdoptionInquiry, InquiryStatus
    inquiries = AdoptionInquiry.objects.filter(listing=listing, status=InquiryStatus.ACTIVE)
    closing = inquiries.filter(reserved_at__isnull=True).count()
    reserved = inquiries.filter(reserved_at__isnull=False).exists()
    lines = []
    if closing:
        lines.append(f"{_n(closing, 'open inquiry', 'open inquiries')} will be closed and each adopter told.")
    if reserved:
        lines.append("The reserved adopter's inquiry stays open: a takedown never reverses an adoption "
                     "already under way.")
    lines += ["It leaves the feed and its detail page. The poster still sees it, marked as removed.",
              "The poster is told it was removed."]
    if closing:
        lines.append("Restoring it later does not reopen the inquiries it closed.")
    return lines


def _report_consequences(report):
    from django.db.models import Q

    from sagip.models import MatchStatus, OfferStatus, ReportMatch, ReportOffer, RescueCase, StrayStatus
    case = RescueCase.objects.filter(report_id=report.pk, expired_at__isnull=True).first()
    claim_ends = report.status == StrayStatus.CLAIMED and case is not None
    offers = ReportOffer.objects.filter(report=report)
    expiring = offers.filter(status=OfferStatus.OPEN).count()
    if claim_ends:
        expiring += offers.filter(status=OfferStatus.MATCHED).count()
    matches = ReportMatch.objects.filter(Q(report=report) | Q(matched_report=report),
                                         status=MatchStatus.SUGGESTED).count()
    lines = []
    if claim_ends:
        lines.append("The claim will end and the rescuer is told.")
    elif report.status in (StrayStatus.RESCUED, StrayStatus.SAFE, StrayStatus.RESOLVED):
        lines.append("The animal is already in someone's care, and that stays with them.")
    if expiring:
        lines.append(f"{_n(expiring, 'offer', 'offers')} will expire.")
    if matches:
        lines.append(f"{_n(matches, 'suggested match', 'suggested matches')} will be dismissed.")
    lines.append("It leaves the map, the counts and its public page.")
    return lines


def _story_consequences(story):
    return ["It will be hidden from the feed. The post and its photos are kept, and the author sees a "
            "hidden banner on it.",
            "The author is not told."]  # MO7 · said plainly until step 9 sends a notice


_RECORDS_ONLY = "Records your decision only."


def _account_consequences(account):
    return [_RECORDS_ONLY, "It does not suspend the account. Suspend from the member's record if that is needed."]


def _qr_consequences(qr):
    from shelter.models import QrStatus
    return [_RECORDS_ONLY, "The QR stays live and donors can still see it." if qr.status == QrStatus.LIVE
            else "The QR is not changed."]


def _shift_consequences(shift):
    from volunteer.models import ShiftStatus
    return [_RECORDS_ONLY, "The shift stays open for signups." if shift.status == ShiftStatus.OPEN
            else "The shift is not changed."]


HANDLED = "Mark handled"
ALREADY_HIDDEN = ["Already hidden. Nothing more will change."]


@dataclass
class TargetSpec:
    model: str | None                 # "app.Model"; None = no model exists to point at
    pk_field: str | None
    describe: Callable | None
    take_down: Callable | None = None  # (obj, reviewer) -> bool, True only if it changed something
    effect: str | None = None          # the `side_effects` key a successful take_down reports
    consequences: Callable | None = None  # (obj) -> list[str] · what actioning will do, said before the click
    action_label: str | None = None    # the take-down button, named for the type: "Remove listing"

    @property
    def supported(self):
        return self.model is not None

    def fetch(self, target_id):
        return apps.get_model(self.model).objects.filter(**{self.pk_field: target_id}).first()

    def exists(self, target_id):
        return apps.get_model(self.model).objects.filter(**{self.pk_field: target_id}).exists()


# ⚠️ `message` has no model (messaging is Phase 2). A flag of that type is describable but not
# resolvable, which is a different state from "the row was deleted".
REGISTRY = {
    FlagTarget.ACCOUNT: TargetSpec("accounts.Account", "account_id", _describe_account,
                                   consequences=_account_consequences),
    FlagTarget.LISTING: TargetSpec("listings.AdoptionListing", "listing_id", _describe_listing,
                                   _hide_listing, "listing_hidden", _listing_consequences, "Remove listing"),
    FlagTarget.REPORT: TargetSpec("sagip.StrayReport", "report_id", _describe_report,
                                  _hide_report, "report_hidden", _report_consequences, "Remove report"),
    FlagTarget.QR: TargetSpec("shelter.DonationQr", "donation_qr_id", _describe_qr,
                              consequences=_qr_consequences),
    FlagTarget.STORY: TargetSpec("community.StoryPost", "story_id", _describe_story,
                                 _hide_story, "story_hidden", _story_consequences, "Hide story"),
    FlagTarget.SHIFT: TargetSpec("volunteer.VolunteerShift", "shift_id", _describe_shift,
                                 consequences=_shift_consequences),
    FlagTarget.MESSAGE: TargetSpec(None, None, None),
}


def target_exists(target_type, target_id):
    """AT28 · a flag must name a row that exists as the type it claims, or flags on nothing pile up in the
    moderators' queue. An unsupported type (`message`) cannot be checked and is accepted."""
    spec = REGISTRY.get(target_type)
    return spec is None or not spec.supported or spec.exists(target_id)


def describe_target(target_type, target_id):
    """Describe what a flag points at. A missing target is NORMAL (it is often why something was flagged),
    so `gone` is an answer, never an error. A describer that fails on an odd row degrades to a plain
    message and is logged; a database error is NOT swallowed into `gone`, because telling a moderator the
    target no longer exists when the database blinked invites them to dismiss a live flag."""
    spec = REGISTRY.get(target_type)
    if spec is None or not spec.supported:
        return {"kind": target_type, "state": "unsupported", "label": "This target type has no surface yet."}
    obj = spec.fetch(target_id)
    if obj is None:
        return {"kind": target_type, "state": "gone", "label": "Target no longer exists."}
    try:
        view = spec.describe(obj)
    except Exception:
        logger.exception("could not describe a %s target", target_type)
        view = _view("", DEGRADED)
    return {"kind": target_type, "state": "present", "label": view["title"], **view,
            "on_action": _on_action(spec, obj, view["hidden"])}


def _on_action(spec, obj, hidden):
    """What the Action button will do for this target, so the console can say it before the click.

    A type with no take-down offers `Mark handled`, never a remove button, and says it records the decision
    only (MO5: it used to read as if it did something). An already hidden target offers the same, so a
    second flag on a taken-down listing does not invite taking it down again (MO4's visible edge)."""
    if spec.take_down is None:
        return {"label": HANDLED, "changes": False, "already_done": False, "consequences": _said(spec, obj)}
    if hidden:
        return {"label": HANDLED, "changes": False, "already_done": True, "consequences": ALREADY_HIDDEN}
    return {"label": spec.action_label, "changes": True, "already_done": False, "consequences": _said(spec, obj)}


def _said(spec, obj):
    try:
        return spec.consequences(obj)
    except Exception:
        logger.exception("could not work out the consequences of a %s", spec.model)
        return ["We couldn't work out what this will change."]
