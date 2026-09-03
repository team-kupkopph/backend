"""US-B1 · badge awarding + impact aggregation.

D-S6-3: badges are event-awarded and sweep-reconciled, never counted in a column. Awarding is
an idempotent insert (uq_account_badge absorbs replays), so `award_badges_for` runs both at the
qualifying event and in the nightly catch-up sweep without ever double-awarding. Impact stats
are aggregated on read from the existing tables (§6.6) — no counters stored.

D-S6-5: `badge_earned` is in-app only (push:false). A badge is a celebration, not something to
buzz a phone for — pushing it would cheapen push for a match or a pledge.

D-S6-2: criteria are shift-agnostic — any completed Kawang-Gawa shift counts, not walks only.
"""
from listings.models import AdoptionListing, ListingStatus
from sagip.models import RescueCase

from common.analytics import emit
from notifications.service import notify

from .models import AccountBadge, Badge, NeedPledge, PledgeStatus


def impact_counts(account):
    """The four impact aggregates, computed on read from existing tables (§6.6). Reuses the
    US-V5 reliability query for the completed-shift count rather than forking a second one."""
    from volunteer.reliability import reliability_for
    return {
        "shifts_completed": reliability_for(account)["shifts_completed"],
        "rescues_helped": RescueCase.objects.filter(claimed_by_account=account,
                                                    resolved_at__isnull=False).count(),
        "pets_rehomed": AdoptionListing.objects.filter(posted_by=account,
                                                       status=ListingStatus.ADOPTED).count(),
        "pledges_delivered": NeedPledge.objects.filter(pledger_account=account,
                                                       status=PledgeStatus.DELIVERED).count(),
    }


def _earned_codes(c):
    earned = set()
    if c["shifts_completed"] >= 1:
        earned.add("first_shift")
    if c["shifts_completed"] >= 10:
        earned.add("shifts_10")
    if c["shifts_completed"] >= 50:
        earned.add("shifts_50")
    if c["rescues_helped"] >= 1:
        earned.add("first_rescue")
    if c["pets_rehomed"] >= 1:
        earned.add("rehomed_1")
    if c["pets_rehomed"] >= 5:
        earned.add("rehomed_5")
    if c["shifts_completed"] >= 1 and c["rescues_helped"] >= 1 and c["pets_rehomed"] >= 1:
        earned.add("bayani")
    return earned


def award_badges_for(account):
    """Award any newly-earned badges to `account`, idempotently. Returns the list of newly
    awarded badge_codes; fires one in-app `badge_earned` per new badge (D-S6-5, no push)."""
    earned = _earned_codes(impact_counts(account))
    # Only award badges that actually exist in the catalog — you can't earn a badge that
    # isn't seeded. Also keeps this a no-op (never an FK error) if the catalog is absent.
    catalog = set(Badge.objects.filter(pk__in=earned).values_list("pk", flat=True))
    have = set(AccountBadge.objects.filter(account=account).values_list("badge_id", flat=True))
    awarded = []
    for code in sorted(catalog - have):
        # A concurrent award is absorbed by uq_account_badge; only the real insert notifies.
        _, created = AccountBadge.objects.get_or_create(account=account, badge_id=code)
        if not created:
            continue
        awarded.append(code)
        badge = Badge.objects.filter(pk=code).first()
        notify(account, "badge_earned", title="New badge!",
               body=f"You earned “{badge.name}”." if badge else "You earned a badge.",
               data={"badge_code": code})
        emit("badge_earned", badge_code=code)
    return awarded
