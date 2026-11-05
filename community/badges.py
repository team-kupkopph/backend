"""US-B1 · badge awarding + impact aggregation.

D-S6-3: badges are event-awarded and sweep-reconciled, never counted in a column. Awarding is
an idempotent insert (uq_account_badge absorbs replays), so `award_badges_for` runs both at the
qualifying event and in the nightly catch-up sweep without ever double-awarding. Impact stats
are aggregated on read from the existing tables (§6.6) — no counters stored.

D-S6-5: `badge_earned` is in-app only (push:false). A badge is a celebration, not something to
buzz a phone for — pushing it would cheapen push for a match or a pledge.

D-S6-2: criteria are shift-agnostic — any completed Kawang-Gawa shift counts, not walks only.

IB2: a badge never gets a vote on the work that earned it. Writers call `award_after_commit`,
never `award_badges_for`: a failing award used to roll back an adoption, or answer 500 for
attendance that had already saved. The sweep repairs anything missed.
"""
import logging

from django.db import transaction

from common.analytics import emit
from listings.models import AdoptionListing, ListingStatus
from notifications.models import Notification
from notifications.service import notify
from sagip.models import RescueCase

from .models import AccountBadge, Badge, NeedPledge, PledgeStatus

logger = logging.getLogger("kupkop.badges")

# The completed shifts each shift badge needs. Bayani's shift leg is judged separately: it has
# two other legs, which `reconcile_shift_badges` must not re-open.
SHIFT_FLOORS = {"first_shift": 1, "shifts_10": 10, "shifts_50": 50}


def impact_counts(account):
    """The four impact aggregates, computed on read from existing tables (§6.6). Reuses the
    US-V5 reliability query for the completed-shift count rather than forking a second one."""
    from volunteer.reliability import reliability_for
    return {
        "shifts_completed": reliability_for(account)["shifts_completed"],
        # D13 · a rescue of your own report, or an animal "placed" with yourself, is real work but
        # not a badge: otherwise report → claim → resolve mints one in a minute.
        "rescues_helped": (RescueCase.objects.filter(claimed_by_account=account,
                                                     resolved_at__isnull=False)
                           .exclude(report__reporter_account=account).count()),
        "pets_rehomed": (AdoptionListing.objects.filter(posted_by=account,
                                                        status=ListingStatus.ADOPTED)
                         .exclude(adopted_by_account=account).count()),
        "pledges_delivered": NeedPledge.objects.filter(pledger_account=account,
                                                       status=PledgeStatus.DELIVERED).count(),
    }


def _earned_codes(c):
    earned = {code for code, floor in SHIFT_FLOORS.items() if c["shifts_completed"] >= floor}
    if c["rescues_helped"] >= 1:
        earned.add("first_rescue")
    if c["pets_rehomed"] >= 1:
        earned.add("rehomed_1")
    if c["pets_rehomed"] >= 5:
        earned.add("rehomed_5")
    if c["shifts_completed"] >= 1 and c["rescues_helped"] >= 1 and c["pets_rehomed"] >= 1:
        earned.add("bayani")
    return earned


def award_badges_for(account, source="sweep"):
    """Award any newly-earned badges to `account`, idempotently. Returns the list of newly
    awarded badge_codes; fires one in-app `badge_earned` per new badge (D-S6-5, no push).

    `source` is where the award came from: `shift`, `rescue`, `rehome` or `sweep`. A sweep award
    is a hook that missed, so the event is how a hook that stopped working gets noticed (IB18).

    IB3 · a badge and its notice land together or not at all: a badge that outlived a failed
    notice was held, so no rerun could ever tell its owner. One failure stops only that badge.
    """
    earned = _earned_codes(impact_counts(account))
    # Only award badges that actually exist in the catalog — you can't earn a badge that
    # isn't seeded. Also keeps this a no-op (never an FK error) if the catalog is absent.
    catalog = set(Badge.objects.filter(pk__in=earned).values_list("pk", flat=True))
    have = set(AccountBadge.objects.filter(account=account).values_list("badge_id", flat=True))
    awarded = []
    for code in sorted(catalog - have):
        try:
            with transaction.atomic():
                # A concurrent award is absorbed by uq_account_badge; only the real insert notifies.
                _, created = AccountBadge.objects.get_or_create(account=account, badge_id=code)
                if not created:
                    continue
                badge = Badge.objects.filter(pk=code).first()
                notify(account, "badge_earned", title="New badge!",
                       body=f"You earned “{badge.name}”." if badge else "You earned a badge.",
                       data={"badge_code": code})
        except Exception:
            logger.exception("badge %s not awarded to %s", code, account.pk)
            continue
        awarded.append(code)
        emit("badge_earned", badge_code=code, source=source)
    return awarded


def award_after_commit(account, source):
    """What a qualifying writer calls (IB2). Runs once the writer's transaction commits, so a
    rollback awards nothing and no phantom `badge_earned` is counted, and it never raises."""
    def run():
        try:
            award_badges_for(account, source=source)
        except Exception:
            logger.exception("badge award failed for %s", account.pk)
    transaction.on_commit(run)


def reconcile_shift_badges(account):
    """IB1 / IQ1 · take back the shift badges `account` no longer qualifies for, and their
    notices, silently. Call inside the transaction that lowered the count, so a badge can't
    outlive its shift. Returns the revoked badge_codes.

    Shift badges only, and Bayani only when no shift is left: D13's "badges already awarded are
    kept" still covers rescue badges, and a kept Bayani must not be re-judged on those legs.
    """
    from volunteer.reliability import reliability_for
    shifts = reliability_for(account)["shifts_completed"]
    gone = {code for code, floor in SHIFT_FLOORS.items() if shifts < floor}
    if shifts < 1:
        gone.add("bayani")
    held = AccountBadge.objects.filter(account=account, badge_id__in=gone)
    revoked = sorted(held.values_list("badge_id", flat=True))
    held.delete()
    Notification.objects.filter(account=account, type="badge_earned",
                                data__badge_code__in=revoked).delete()
    for code in revoked:
        transaction.on_commit(lambda code=code: emit("badge_revoked", badge_code=code,
                                                     reason="attendance_undo"))
    return revoked

