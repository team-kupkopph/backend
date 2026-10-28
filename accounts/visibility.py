"""AS5 / X5 · who is publicly visible: one predicate, written once (plan Task 5).

The review found three spellings of "an account whose content the public may see": needs, shifts, the
donation QR and Sagip alerts filtered on an ACTIVE account, while adoption listings and stories excluded
only DELETED, so a SUSPENDED poster (the person staff just acted against) stayed fully public. This is the
one definition: **only an `active` account is public**. Suspended and deleted are not; reinstating shows
everything again because nothing was deleted.

`accounts/tests/test_visibility_guard.py` fails if a new relation-prefixed account-status filter is written
anywhere else, so this cannot drift back into several spellings.
"""
from django.db.models import Q

from accounts.models import AccountStatus


def public_account_q(prefix=""):
    """A `Q` that keeps rows whose account (reached through `prefix`, e.g. `"posted_by__"`) is public."""
    return Q(**{f"{prefix}status": AccountStatus.ACTIVE})


def account_is_public(account):
    """The per-account form of `public_account_q`."""
    return account.status == AccountStatus.ACTIVE
