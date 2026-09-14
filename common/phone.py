"""Philippine mobile-number normalisation (F12 · dev/onboarding-validation.md rule 5).

A PH mobile is ten digits after the +63 country code, the first of them a 9. Users type
it every which way — `0917 123 4567`, `+63 917 123 4567`, `63-917-123-4567` — so the
canonical E.164 form (`+639171234567`) is computed HERE, once, and every store and every
uniqueness check runs on that. Landlines (`02…`, `+632…`) never normalise: they are not
SMS-reachable, and the OTP we send would go nowhere.
"""
import re

_NOT_DIGIT = re.compile(r"\D")

INVALID_PH_MOBILE_MESSAGE = "Enter a Philippine mobile number, e.g. 0917 123 4567"


def normalize_ph_mobile(raw):
    """Return `+639XXXXXXXXX` for any accepted spelling of a PH mobile, else None.

    Accepted: `09…` (11 digits), `9…` (10 digits), `+639…` / `639…` (12 digits), with any
    spaces, dashes or parentheses between the digits. A leading `+` is only meaningful
    before `63`; anything else that isn't a digit is stripped.
    """
    if not raw:
        return None
    digits = _NOT_DIGIT.sub("", str(raw))
    if digits.startswith("63") and len(digits) == 12:
        national = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        national = digits[1:]
    elif len(digits) == 10:
        national = digits
    else:
        return None
    if not national.startswith("9"):
        return None
    return "+63" + national
