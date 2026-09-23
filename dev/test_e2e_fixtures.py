"""The e2e fixture's shelter profile must be one the tier-aware code recognises.

`ShelterProfile.tier` is a choices field (`community_rescue` / `registered_ngo`). A profile
created with any other value is accepted by the ORM but rejected by everything downstream:
`/me` serves it verbatim, and the mobile `ShelterTier` type, `ShelterProfileScreen`'s
`isTier1`, the ₱500 fee cap and the tier-2 upgrade's `tier1_incomplete` check all fail to
match it. The fixture is the only thing that creates this row, so it is where the invariant
is checked.
"""
import e2e_fixtures
import pytest
from django.core.exceptions import ValidationError

from shelter.models import ShelterProfile, ShelterTier

pytestmark = pytest.mark.django_db


def _run(settings, capsys):
    settings.DEBUG = True  # pytest-django forces it off; the fixture refuses to run without it
    e2e_fixtures.main()
    capsys.readouterr()  # the exports are stdout; keep the throwaway passwords out of the log


def test_a_fresh_shelter_profile_is_valid_and_on_a_real_tier(settings, capsys):
    _run(settings, capsys)
    profile = ShelterProfile.objects.get(account__email=e2e_fixtures.SHELTER)
    assert profile.tier in ShelterTier.values
    try:
        profile.full_clean()
    except ValidationError as exc:
        pytest.fail(f"fixture profile does not validate: {exc.message_dict}")


def test_an_existing_profile_with_an_invalid_tier_is_corrected(settings, capsys):
    # The dev DB already holds this row with tier "1" from earlier runs; get_or_create's
    # defaults never touch an existing row, so the fixture has to repair it explicitly.
    _run(settings, capsys)
    ShelterProfile.objects.filter(account__email=e2e_fixtures.SHELTER).update(tier="1")
    _run(settings, capsys)
    profile = ShelterProfile.objects.get(account__email=e2e_fixtures.SHELTER)
    assert profile.tier == ShelterTier.COMMUNITY_RESCUE


def test_the_e2e_shelter_is_verified_so_its_shift_is_public(settings, capsys):
    _run(settings, capsys)
    from volunteer.visibility import public_shifts
    assert public_shifts().filter(shelter_account__email=e2e_fixtures.SHELTER).exists()
