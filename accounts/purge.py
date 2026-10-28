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
  * A shelter profile's ORGANISATION fields (name, type, tier, registration, description,
    logo, website). They describe the organisation, not a person, and donors' records name
    the shelter by them; its personal fields are scrubbed (PR-6, below).

Mirrors `verifications/purge.py`'s shape: a plain, idempotent, unit-testable function that a
management command invokes on a schedule (US-F0's cron-over-Celery decision).
"""
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounts.data_map import DATA_MAP, SHELTER_PERSONAL_FIELDS, Delete, Keep  # noqa: F401
from accounts.models import Account, AccountStatus
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

    # Everything else that points at this account is decided in one place — accounts/data_map.py,
    # which a guard test forces to cover every foreign key. The comments on each decision live
    # there: what is deleted (a preference, a secret, a privately owned pet), what is scrubbed
    # inside a record that stays (a pledge note, a shelter's contact person, a payee name), and
    # what is kept and why.
    _erase_related(account)

    return account


def _pet_photo_files(account):
    from common.uploads import own_upload_url_error
    from listings.models import PetPhoto
    urls = PetPhoto.objects.filter(pet__owner_account=account).values_list("url", flat=True)
    # PP5 · ONLY this account's own `listing_photo` keys. A pet's photo can be the previous
    # owner's (a transfer keeps them) or, on rows written before the rule, any URL at all; the
    # bucket key is derived from the URL, so a crafted one would delete a stranger's file.
    return [u for u in urls if u and own_upload_url_error(u, account, "listing_photo") is None]


def _qr_image_files(account):
    from shelter.models import DonationQr
    from shelter.qr_rules import donation_qr_url_error
    urls = DonationQr.objects.filter(account=account).values_list("qr_image_url", flat=True)
    # ONLY this account's own `donation_qr` keys, never a legacy foreign URL (the AT21 rule).
    return [u for u in urls if u and donation_qr_url_error(u, account) is None]


#: Stored objects behind rows the map erases. Collected BEFORE the rows go (afterwards nothing
#: can find them), deleted AFTER, and only if nothing else still points at the same file.
_FILE_SOURCES = {"listings.Pet": _pet_photo_files, "shelter.DonationQr": _qr_image_files}


def _file_is_still_used(url):
    """A pet's photo is COPIED from the adoption listing's photo (listings/views.py), so the two
    rows can name one stored file. Deleting it because the adopter left would break a poster's
    still-public listing."""
    from listings.models import AdoptionListingPhoto, PetPhoto
    return (AdoptionListingPhoto.objects.filter(url=url).exists()
            or PetPhoto.objects.filter(url=url).exists())


def _erase_related(account):
    from django.apps import apps

    files = []
    for (label, field), entry in DATA_MAP.items():
        rule = entry.erasure
        if isinstance(rule, Keep):
            continue
        if label in _FILE_SOURCES:
            files += _FILE_SOURCES[label](account)
        rows = apps.get_model(label).objects.filter(**{field: account})
        if isinstance(rule, Delete):
            rows.delete()
        else:
            rows.update(**rule.values)
    for url in dict.fromkeys(files):          # de-duplicated, order kept
        if not _file_is_still_used(url):
            delete_object(url, visibility=PUBLIC)


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
