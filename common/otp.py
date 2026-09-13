import hashlib
import secrets

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from common.senders import get_sender
from verifications.models import VerificationCode


class CodeInvalid(Exception):
    def __init__(self, attempts_left):
        self.attempts_left = attempts_left


class CodeExpired(Exception):
    pass


class CodeLocked(Exception):
    pass


def _hash(code):
    return hashlib.sha256(code.encode()).hexdigest()


def issue_code(account, *, channel, purpose):
    code = f"{secrets.randbelow(1_000_000):06d}"
    ttl = timezone.timedelta(minutes=settings.OTP_TTL_MINUTES)
    VerificationCode.objects.filter(account=account, purpose=purpose,
                                    consumed_at__isnull=True).delete()
    VerificationCode.objects.create(
        account=account, channel=channel, purpose=purpose, code_hash=_hash(code),
        max_attempts=settings.OTP_MAX_ATTEMPTS, expires_at=timezone.now() + ttl,
    )
    get_sender().send(channel=channel, to=account.email if channel == "email" else account.phone, purpose=purpose,
                      code=code)
    return code


def _validate(account, *, purpose, code):
    """Every check a code must pass, and the row if it passes them.

    Shared by `verify_code` and `check_code` rather than duplicated, so the two can never
    answer differently — that equivalence is load-bearing: `check_code` exists to preview the
    answer `verify_code` will give, and a check that were more lenient would send a user on to
    type a new password against a code that is about to be refused.

    A WRONG code costs an attempt here, exactly as it always has. A correct one costs nothing.
    """
    row = (VerificationCode.objects.filter(account=account, purpose=purpose,
                                           consumed_at__isnull=True)
           .order_by("-created_at").first())
    if row is None:
        raise CodeInvalid(attempts_left=0)
    if row.attempts >= row.max_attempts:
        raise CodeLocked()
    if row.expires_at < timezone.now():
        raise CodeExpired()
    if row.code_hash != _hash(code):
        row.attempts = F("attempts") + 1
        row.save(update_fields=["attempts"])
        row.refresh_from_db(fields=["attempts"])
        raise CodeInvalid(attempts_left=max(0, row.max_attempts - row.attempts))
    return row


def check_code(account, *, purpose, code):
    """Validate a code WITHOUT consuming it, so a UI can report a bad code at the step that
    collects it instead of after the user has done more work.

    ⚠️ The deliberate omission is `consumed_at`. The caller is expected to come back with
    `verify_code` for the real operation — this only answers "would that work right now?".
    A code can still expire between the two calls; the flow must handle that, and the mobile
    reset screens do by sending the user back to the code step.
    """
    _validate(account, purpose=purpose, code=code)


def verify_code(account, *, purpose, code):
    row = _validate(account, purpose=purpose, code=code)
    row.consumed_at = timezone.now()
    row.save(update_fields=["consumed_at"])
