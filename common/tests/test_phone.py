import pytest

from common.phone import normalize_ph_mobile

# F12 · dev/onboarding-validation.md rule 5: a PH mobile is 10 digits after +63, first
# digit 9; every accepted spelling normalises to E.164 BEFORE the uniqueness check.


@pytest.mark.parametrize("raw", [
    "09171234567",
    "9171234567",
    "+639171234567",
    "639171234567",
    "0917 123 4567",
    "+63 917 123 4567",
    "63 917 123 4567",
    "0917-123-4567",
    "+63 (917) 123-4567",
    "  09171234567  ",
])
def test_accepted_spellings_normalise_to_e164(raw):
    assert normalize_ph_mobile(raw) == "+639171234567"


@pytest.mark.parametrize("raw", [
    "0281234567",        # Manila landline — the F12 repro
    "+6328123 4567",     # landline via +63
    "6328 1234567",
    "0812345678",        # not a 9-series number
    "+638123456789",
    "0917123456",        # 9 digits after 0 — too short
    "091712345678",      # 11 digits after 0 — too long
    "+63917123456",
    "+19171234567",      # wrong country
    "917123456a",        # letters
    "",
    "   ",
    "+63",
])
def test_everything_else_is_rejected(raw):
    assert normalize_ph_mobile(raw) is None


def test_none_is_rejected():
    assert normalize_ph_mobile(None) is None
