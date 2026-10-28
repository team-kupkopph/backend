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
    elif purpose == "stepup":
        subject = "Confirm it's you on Kupkop PH"
        opener = "Your Kupkop PH code to confirm a sensitive action is:"
        closer = ("If you didn't ask for this, someone may be using your account: change your "
                  "password and your email's password.")
    elif purpose == "email":
        subject = "Confirm your new Kupkop PH email"
        opener = "Your Kupkop PH code to confirm this email address is:"
        closer = ("If you didn't ask to use this address on Kupkop PH, ignore this message — "
                  "nothing will change.")
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


def mask_email(address: str) -> str:
    """`n***@example.com`: enough for the owner to recognise it, not enough to mail it."""
    local, _, domain = (address or "").partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def mask_phone(number: str) -> str:
    """`+6391***0000`: enough for the owner to recognise it, not the number."""
    number = number or ""
    return f"{number[:5]}***{number[-4:]}" if len(number) >= 10 else "***"


def compose_email_changed_notice(new_address: str) -> tuple[str, str]:
    """`(subject, body)` told to the OLD address when the account's email is changed (T13).

    It exists so that a change made from a hijacked session cannot be silent: the person who still
    owns this inbox is told, with the one thing that helps (reset the password, which also ends
    every session). The new address is masked, because the reader may not be who changed it."""
    subject = "Your Kupkop PH email was changed"
    body = (f"The email on your Kupkop PH account was just changed to {mask_email(new_address)}.\n\n"
            f"If that was you, there is nothing to do.\n\n"
            f"If it wasn't, someone else has your account. Use \"Forgot password\" on the sign-in "
            f"screen with the new address, or contact us, and change your password straight away.\n\n"
            f"— Kupkop PH")
    return subject, body
