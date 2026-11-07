"""Review steps 12 and 13 (MO10, MO9) · people who keep turning up, and the flags the system raises about them.

⚠️ The queue works one flag at a time, so a person with ten removed listings looked clean: `flags_about` on a member covered
`account` flags only. This reads every flag about a person's own content through the registry's `person` (the same mapping "report /
block this person" uses), counts what was actually taken down, and raises a flag of its own when it keeps happening.

NEVER AUTO-SUSPENDS, and never revokes a badge. MQ6: a rule raises a flag for a human; the flag preserves a deterrent and leaves the
escalation to a person (Tech Spec §6.7 gives the reason for the withdrawal rule, and it holds for this one too).

Two rules, both idempotent, both with a derived count (nothing is stored but the flag):
  - `repeat_withdrawal` (§6.7): the adopter's 3rd consecutive withdrawn inquiry. Consecutive resets on an inquiry that ends adopted.
  - `repeat_takedown` (MQ6): the 3rd distinct thing of theirs taken down within 90 days.
A rule never raises a second flag while one it raised is still open or being reviewed, and after one is decided it needs three MORE
before it raises again, so a dismissed flag is not re-raised by the next sweep.
"""
import logging
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from moderation import telemetry
from moderation.models import DECIDABLE, FlagStatus, FlagTarget, ModerationFlag, ReasonCode

logger = logging.getLogger("kupkop.moderation")

RULE_WITHDRAWAL = "repeat_withdrawal"
RULE_TAKEDOWN = "repeat_takedown"
RULES = {RULE_WITHDRAWAL: "Repeat withdrawals", RULE_TAKEDOWN: "Repeat takedowns"}

THRESHOLD = 3            # matches §6.5's volunteer no-show rule and §6.7: one tolerance number across the app
TAKEDOWN_WINDOW_DAYS = 90


def about_person(account):
    """A filter for every flag about `account` or about anything of theirs that can be taken down or reported to a person.
    A rescue report is deliberately absent: its reporter is hidden from everyone else, and resolving it here would unmask them."""
    from community.models import StoryPost
    from listings.models import AdoptionInquiry, AdoptionListing
    from shelter.models import DonationQr
    from volunteer.models import VolunteerShift, VolunteerSignup
    own = {
        FlagTarget.LISTING: AdoptionListing.objects.filter(posted_by=account),
        FlagTarget.STORY: StoryPost.objects.filter(author_account=account),
        FlagTarget.QR: DonationQr.objects.filter(account=account),
        FlagTarget.SHIFT: VolunteerShift.objects.filter(shelter_account=account),
        FlagTarget.INQUIRY: AdoptionInquiry.objects.filter(adopter_account=account),
        FlagTarget.SIGNUP: VolunteerSignup.objects.filter(volunteer_account=account),
    }
    keep = Q(target_type=FlagTarget.ACCOUNT, target_id=account.pk)
    for kind, rows in own.items():
        keep |= Q(target_type=kind, target_id__in=rows.values("pk"))
    return keep


def takedown_count(account, now=None, days=TAKEDOWN_WINDOW_DAYS):
    """How many DISTINCT things of theirs were taken down in the window. Distinct, because three reports about one listing close as three
    actioned flags and are one takedown."""
    since = (now or timezone.now()) - timedelta(days=days)
    rows = (ModerationFlag.objects.filter(about_person(account), status=FlagStatus.ACTIONED, reviewed_at__gte=since)
            .exclude(decision_reason_code="").values_list("target_type", "target_id").distinct())
    return rows.count()


def _live(account, rule):
    return ModerationFlag.objects.filter(reporter_account__isnull=True, target_type=FlagTarget.ACCOUNT, target_id=account.pk,
                                         rule=rule, status__in=DECIDABLE).exists()


def raise_system_flag(account, rule, reason):
    """Raise a flag the platform itself noticed. None when one for this rule is already open or under review."""
    if _live(account, rule):
        return None
    flag = ModerationFlag.objects.create(
        reporter_account=None, target_type=FlagTarget.ACCOUNT, target_id=account.pk, reason=reason,
        reason_code=ReasonCode.OTHER, rule=rule, target_snapshot={"title": account.display_name or "", "body": "", "photos": []})
    telemetry.system_flag_raised(rule)
    logger.info("system flag raised (rule=%s)", rule)
    return flag


def _last_raised(account, rule):
    return (ModerationFlag.objects.filter(reporter_account__isnull=True, target_type=FlagTarget.ACCOUNT, target_id=account.pk,
                                          rule=rule).order_by("-created_at").first())


def escalate_if_repeat_takedown(account, now=None):
    """Called right after a takedown lands. Raises `repeat_takedown` at the third distinct takedown in 90 days."""
    if account is None:
        return None
    count = takedown_count(account, now)
    if count < THRESHOLD:
        return None
    previous = _last_raised(account, RULE_TAKEDOWN)
    if previous is not None:
        since = ModerationFlag.objects.filter(about_person(account), status=FlagStatus.ACTIONED, reviewed_at__gte=previous.created_at)
        if since.exclude(decision_reason_code="").values_list("target_type", "target_id").distinct().count() < THRESHOLD:
            return None                                  # a decided flag needs three MORE before it is raised again
    return raise_system_flag(
        account, RULE_TAKEDOWN, f"{count} of this person's posts were taken down in the last {TAKEDOWN_WINDOW_DAYS} days.")


def _withdrawal_run(account):
    """The adopter's current run of withdrawals they made themselves, newest first (a list of decided-at times). Consecutive: it stops at
    an inquiry that ended adopted. An inquiry the poster declined or the system closed neither counts nor resets it (§6.7)."""
    from listings.models import AdoptionInquiry, EndReason, InquiryStatus
    run = []
    decided = (AdoptionInquiry.objects.filter(adopter_account=account, decided_at__isnull=False)
               .order_by("-decided_at").values_list("status", "end_reason", "decided_at"))
    for status, end_reason, decided_at in decided:
        if status == InquiryStatus.ADOPTED:
            break
        if status == InquiryStatus.WITHDRAWN and end_reason == EndReason.ADOPTER_WITHDREW:
            run.append(decided_at)
    return run


def raise_repeat_withdrawal_flags():
    """The §6.7 rule, as a sweep: raise `repeat_withdrawal` for each adopter whose run has reached three (or three more since the last
    flag about them was raised). Returns the flags it raised. Safe to run as often as you like."""
    from django.db.models import Count

    from accounts.models import Account
    from listings.models import AdoptionInquiry, EndReason
    candidates = (AdoptionInquiry.objects.filter(end_reason=EndReason.ADOPTER_WITHDREW)
                  .values("adopter_account").annotate(n=Count("pk")).filter(n__gte=THRESHOLD).values_list("adopter_account", flat=True))
    raised = []
    for account in Account.objects.filter(pk__in=list(candidates)):
        run = _withdrawal_run(account)
        previous = _last_raised(account, RULE_WITHDRAWAL)
        fresh = [t for t in run if previous is None or t > previous.created_at]
        if len(run) < THRESHOLD or len(fresh) < THRESHOLD:
            continue
        flag = raise_system_flag(account, RULE_WITHDRAWAL, f"{len(run)} adoption inquiries withdrawn in a row.")
        if flag is not None:
            raised.append(flag)
    return raised
