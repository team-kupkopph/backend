from django.db.models import Q


def public_poster_q():
    """The public-visibility predicate for a listing's poster (§3.5): a **Verified
    Member** (an approved `rescuer` capability) **OR** a **verified shelter** (an
    approved `shelter_org` verification_request). Written once here because Sprint 3
    reuses it — keep both arms in sync with the derivation rule, never a stored flag."""
    return (
        Q(posted_by__capabilities__capability="rescuer",
          posted_by__capabilities__status="approved")
        | Q(posted_by__verifications__type="shelter_org",
            posted_by__verifications__status="approved")
    )


def account_is_verified_rescuer(account):
    """The per-account form of `public_poster_q()`'s predicate — an approved `rescuer`
    capability (Verified Member) OR an approved `shelter_org` verification (verified
    shelter). Sprint 3's claim gate (`listings.permissions.IsVerifiedRescuer`, US-K1) needs a
    single-account boolean rather than a queryset filter; keep both derivations in
    lockstep — the same identity that may list may claim."""
    return (account.capabilities.filter(capability="rescuer", status="approved").exists()
            or account.verifications.filter(type="shelter_org", status="approved").exists())


def account_is_verified_member(account):
    """AQ2 · the Verified Member badge alone (an approved `rescuer` capability), which Reserve and
    Complete require of an adopter. Narrower than account_is_verified_rescuer: a shelter can't
    adopt (decision 3)."""
    return account.capabilities.filter(capability="rescuer", status="approved").exists()
