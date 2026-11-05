"""US-B1 / D-S6-3 · nightly badge reconciliation.

The event hooks (a completed shift, a resolved rescue, a rehomed pet) award immediately, but a
sweep is the safety net: it re-runs `award_badges_for` for every account whose counts say it is
missing a badge, so one missed by a code path (or newly seeded) still lands. Idempotent — awards
nothing already held.

Nightly, as `manage.py run_badge_sweep` (IB23). It lived in the hourly `run_sweeps`, whose own
rule is that a sweep joins it only if a one-hour delay would hurt someone, and a late badge
hurts nobody. It is also batched: an idle run costs a fixed number of queries per
batch, not nine per account.
"""
import logging

from accounts.models import Account, AccountStatus
from common.analytics import emit
from listings.models import AdoptionListing, ListingStatus
from sagip.models import RescueCase
from volunteer.models import SignupStatus, VolunteerSignup

from .badges import award_badges_for, earned_codes, impact_counts_many
from .models import AccountBadge, Badge, NeedPledge, PledgeStatus

logger = logging.getLogger("kupkop.badges")

# Accounts read per round trip: bounds the IN-lists and what is held in memory.
BATCH = 500


def _candidate_account_ids():
    """Accounts with activity a badge can come from: a completed shift, a resolved rescue, an adopted listing, a delivered
    pledge. Pledges were left out while no badge used them (IB20); Generous does (IQ5), so a donor whose event hook missed is
    found here. Only DELIVERED pledges: one that was made or cancelled earns nothing."""
    ids = set()
    ids.update(VolunteerSignup.objects.filter(status=SignupStatus.COMPLETED)
               .values_list("volunteer_account_id", flat=True))
    ids.update(RescueCase.objects.filter(resolved_at__isnull=False)
               .values_list("claimed_by_account_id", flat=True))
    ids.update(AdoptionListing.objects.filter(status=ListingStatus.ADOPTED)
               .values_list("posted_by_id", flat=True))
    ids.update(NeedPledge.objects.filter(status=PledgeStatus.DELIVERED)
               .values_list("pledger_account_id", flat=True))
    return ids


def award_badges():
    """Reconcile badges for every active account with qualifying activity. Returns
    `(touched, failed)`: `touched` is [(account_id, [newly_awarded_codes])] for accounts that
    gained a badge this run, `failed` the ids of accounts whose award raised.

    Only ACTIVE accounts (IB17): a deleted or suspended one can't open the app, so a badge and a
    notice written to it are noise, and it is badged when it comes back. An account raising is
    logged and skipped (IB24): it is retried tomorrow and never stops the others.
    """
    touched, failed, scanned, awarded = [], [], 0, 0
    ids = sorted(_candidate_account_ids(), key=str)
    catalog = list(Badge.objects.all())
    for start in range(0, len(ids), BATCH):
        accounts = list(Account.objects.filter(pk__in=ids[start:start + BATCH],
                                               status=AccountStatus.ACTIVE).order_by("pk"))
        counts = impact_counts_many(accounts)
        held = {}
        for account_id, code in (AccountBadge.objects.filter(account__in=accounts)
                                 .values_list("account_id", "badge_id")):
            held.setdefault(account_id, set()).add(code)
        for account in accounts:
            scanned += 1
            if earned_codes(counts[account.pk], catalog) <= held.get(account.pk, set()):
                continue
            try:
                codes = award_badges_for(account, source="sweep")
            except Exception:
                logger.exception("badge sweep failed for %s", account.pk)
                failed.append(account.pk)
                continue
            if codes:
                touched.append((account.pk, codes))
                awarded += len(codes)
    emit("badge_sweep", accounts=scanned, awarded=awarded, failed=len(failed))
    return touched, failed
