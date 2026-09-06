"""Subject + body for outbound transactional messages, kept out of the sender.

The sender's job is transport. The message copy is a product/design decision that must not
depend on whether we're writing to SES, SMTP or the console — so a new provider added later
gets the same words for free, and a copy change is one edit rather than one per backend.

Only two purposes exist today (`signup`, `reset`); both are 6-digit OTPs. If the app grows a
"welcome" or "notification" email later, they add a case here rather than a new module.
"""
from django.conf import settings


def compose_otp_email(purpose: str, code: str) -> tuple[str, str]:
    """`(subject, body)` for the OTP with `purpose` and value `code`.

    Body is deliberately plain text. HTML mail:
      * needs branding assets, dark-mode variants, and an unsubscribe/footer that the org
        does not have a physical address for yet (RA 10173-relevant when we do);
      * lands in more spam filters than plain text does for a bare 6-digit code;
      * carries a tracking pixel by default in most SDKs — we do not want one on an OTP.

    When the app grows a physical mailing address (part of NPC registration, §12.6's
    closing paragraph), append it here as a footer.
    """
    ttl = settings.OTP_TTL_MINUTES
    if purpose == "signup":
        subject = "Verify your Kupkop PH account"
        opener = "Your Kupkop PH verification code is:"
        closer = "If you didn't ask for this, you can ignore this message."
    elif purpose == "reset":
        subject = "Reset your Kupkop PH password"
        opener = "Your Kupkop PH password reset code is:"
        closer = ("If you didn't ask for this, ignore this message — your password stays "
                  "unchanged.")
    else:
        # A purpose the code does not know about is a programming error, not a runtime one.
        # Better to send SOMETHING than to swallow the send, so the user still gets in and
        # the missing case surfaces to someone who can add it.
        subject = "Your Kupkop PH code"
        opener = "Your Kupkop PH code is:"
        closer = "If you didn't ask for this, you can ignore this message."
    body = (f"{opener}\n\n"
            f"    {code}\n\n"
            f"It expires in {ttl} minutes. Please don't share it with anyone — Kupkop PH "
            f"staff will never ask you for it.\n\n"
            f"{closer}\n\n"
            f"— Kupkop PH")
    return subject, body
