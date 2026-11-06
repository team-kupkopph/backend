"""Review step 7d · who a viewer must not see, and who must not see them (MQ1).

A block is one row (`AccountBlock`) and works in BOTH directions: if I block you, my content leaves your screens too.
That is the half people forget, and without it a blocked person can keep reading what the blocker posts.

The set is small (a person's own blocks plus those against them), so it is read as two cheap queries rather than
joined into every feed query. A guest has none. Callers use it to leave a blocked person's content out of a list, or
to answer a single thing with the same 404 a missing thing gets, so a block never says that it is one.
Never used for rescue reports or offers: an animal in trouble outranks a block (MQ1), and nothing already under way
between the two (an adoption, a claim) is touched."""
from moderation.models import AccountBlock


def hidden_from(viewer):
    """Account ids `viewer` must not see content from, and who must not see theirs: blocked either way round."""
    if viewer is None or not getattr(viewer, "is_authenticated", False):
        return set()
    mine = AccountBlock.objects.filter(blocker=viewer).values_list("blocked_id", flat=True)
    theirs = AccountBlock.objects.filter(blocked=viewer).values_list("blocker_id", flat=True)
    return {*mine, *theirs}
