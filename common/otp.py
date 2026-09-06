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


def verify_code(account, *, purpose, code):
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
    row.consumed_at = timezone.now()
    row.save(update_fields=["consumed_at"])
