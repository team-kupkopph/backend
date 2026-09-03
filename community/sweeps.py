"""US-B1 / D-S6-3 · nightly badge reconciliation.

The event hooks (a completed shift, a resolved rescue, a rehomed pet) award immediately, but a
sweep is the safety net: it re-runs `award_badges_for` for every account with qualifying
activity, so a badge missed by a code path (or newly seeded) still lands. Idempotent — awards
nothing already held.
"""
from listings.models import AdoptionListing, ListingStatus
from sagip.models import RescueCase
from volunteer.models import SignupStatus, VolunteerSignup

from .badges import award_badges_for
from .models import NeedPledge, PledgeStatus


def _candidate_account_ids():
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
    """Reconcile badges for every account with qualifying activity. Returns the list of
    (account_id, [newly_awarded_codes]) for accounts that gained a badge this run."""
    from accounts.models import Account
    touched = []
    for acc in Account.objects.filter(pk__in=_candidate_account_ids()):
        awarded = award_badges_for(acc)
        if awarded:
            touched.append((acc.pk, awarded))
    return touched
