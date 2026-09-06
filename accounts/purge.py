"""US-N2 · the post-grace account purge (§12.7, D-S7-1).

`DELETE /me` (accounts/lifecycle.py) flips the account and revokes its sessions.
`ACCOUNT_PURGE_GRACE_DAYS` later this sweep makes it irreversible — by **scrubbing the PII in
place**, not by deleting the row.

WHY IN PLACE. Two independent reasons, either sufficient:

  * The database refuses a delete. Eight FKs to `account` carry no `ON DELETE` clause, three
    of them `PROTECT` in Django (`rescue_case.claimed_by`, `adoption_inquiry.adopter`,
    `volunteer_signup.volunteer`), so `.delete()` raises rather than cascading.
  * §12.7 wants it. It keeps "non-identifying records needed for welfare integrity" and
    prefers reassigning public content to a "deleted user" over orphaning it. A rescue that
    happened still happened after its rescuer leaves; the count of animals helped must not
    quietly drop because someone exercised their erasure right.

So the sweep erases the PERSON and keeps the RECORD. What gets scrubbed is listed below;
everything else stays attached to the same row, now anonymous.

⚠️ WHAT IS DELIBERATELY *NOT* TOUCHED — each of these looks like PII and is not safe to null:

  * `moderation_flag.reporter_account` — NULL means **"System"** in the admin queue (a
    platform-raised flag, per the Sprint 2 rule honoured in `moderation/admin.py`). Nulling
    a departed person's flags would launder their reports into ones the platform raised
    itself, corrupting the moderation audit trail.
  * `verification_request` and its decision — §12.6 keeps the decision record for the audit
    trail. The identity IMAGES are a separate concern on their own 90-day clock
    (`verifications/purge.py`, US-SEC4); this sweep must not race or duplicate it.
  * Rescue cases, adoptions, completed shifts, stories, pledges, badges — the welfare record.

Mirrors `verifications/purge.py`'s shape: a plain, idempotent, unit-testable function that a
management command invokes on a schedule (US-F0's cron-over-Celery decision).
"""
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounts.models import Account, AccountStatus, Address
from common.storage import PUBLIC, delete_object


def anonymized_email(account):
    """A per-account, permanently unroutable stand-in for the real address.

    `account.email` is `NOT NULL UNIQUE`, so it cannot be blanked — two purged accounts would
    collide on `""`. `.invalid` is reserved by RFC 2606 and can never resolve, so the value
    is inert as well as unique, and the person's original address is freed for reuse.
    """
    return f"deleted-{account.pk}@deleted.kupkop.invalid"


@transaction.atomic
def anonymize_account(account, now=None):
    """Irreversibly scrub one account's PII in place and stamp `anonymized_at`."""
    now = now or timezone.now()

    # The avatar is the one piece of PII living outside the row. Delete the object before
    # dropping the reference, or nothing will ever be able to find it again.
    if account.photo_url:
        delete_object(account.photo_url, visibility=PUBLIC)

    account.email = anonymized_email(account)
    account.phone = None
    account.display_name = "Deleted user"       # §12.7's own words, and what the UI shows
    account.photo_url = ""
    account.password_hash = ""                  # nothing can ever sign in as this account
    account.anonymized_at = now
    # status and deleted_at are deliberately left as they are: the M5 CHECK requires them to
    # move together, and the account is still deleted — it is now merely also anonymous.
    account.save(update_fields=["email", "phone", "display_name", "photo_url",
                                "password_hash", "anonymized_at", "updated_at"])

    # A postal address is PII with no welfare value once the person is gone — unlike a
    # rescue outcome, nothing downstream is made incomplete by removing it.
    Address.objects.filter(account=account).delete()

    return account


def anonymize_expired_accounts(now=None):
    """Anonymise every account whose grace window has closed. Returns the rows touched.

    Idempotent by construction: already-stamped accounts are excluded by the query, so a
    second run is a no-op and a re-run can never re-sentinel an email or move a timestamp.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(days=settings.ACCOUNT_PURGE_GRACE_DAYS)
    expired = Account.objects.filter(status=AccountStatus.DELETED,
                                     deleted_at__lte=cutoff,
                                     anonymized_at__isnull=True)
    return [anonymize_account(account, now=now) for account in expired]
