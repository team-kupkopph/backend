"""The Sagip test plan's personas (dev/test-plan-sagip.md §2) must hold the standing each row
relies on, or a row fails on a gate that is working correctly.

The gates are the product's own predicates, not re-implementations: who a report-time alert
pages (`sagip.alerts.verified_in_city`), who may claim (`account_is_verified_rescuer`) and
who may inquire (an approved `rescuer` capability plus a verified phone). Asserting through
them is what makes a persona drift visible here instead of as a false fail mid-run.
"""
import pytest
import sagip_fixtures
from django.contrib.auth.models import User

from accounts.models import Account, AccountSettings, Address
from adminapi.auth import tokens_for_staff
from listings.models import Pet
from listings.permissions import account_is_verified_rescuer
from sagip.alerts import verified_in_city
from shelter.models import ShelterProfile

pytestmark = pytest.mark.django_db

P = sagip_fixtures.PERSONAS


def _run(settings, *handles):
    settings.DEBUG = True  # pytest-django forces it off; the fixture refuses to run without it
    return sagip_fixtures.seed(list(handles) or None)


def _acct(handle):
    return Account.objects.get(email=P[handle]["email"])


def test_it_refuses_to_run_outside_debug(settings):
    settings.DEBUG = False
    with pytest.raises(SystemExit):
        sagip_fixtures.seed(None)
    assert not Account.objects.filter(email__endswith="@kupkop.invalid").exists()


def test_every_address_is_unresolvable():
    assert P and all(p["email"].endswith(".invalid") for p in P.values())


def test_every_persona_is_seeded_with_settings_and_no_password(settings):
    _run(settings)
    assert sagip_fixtures.DEFAULT_HANDLES
    for handle in sagip_fixtures.DEFAULT_HANDLES:
        a = _acct(handle)
        assert AccountSettings.objects.filter(account=a).exists(), handle
        assert a.password_hash == "", f"{handle} must sign in through seed_tokens only"
        assert a.email_verified_at is not None, handle


def test_the_newcomer_is_only_created_when_asked_for(settings):
    _run(settings)
    assert not Account.objects.filter(email=P["RES-N"]["email"]).exists()
    _run(settings, "RES-N")
    assert account_is_verified_rescuer(_acct("RES-N"))


def test_a_marikina_urgent_report_pages_exactly_the_planned_people(settings):
    _run(settings)
    paged = set(verified_in_city("Marikina").values_list("email", flat=True))
    for handle in ("RES-A", "RES-B", "SHL-V"):
        assert P[handle]["email"] in paged, handle
    for handle in ("RES-P", "RES-S", "SHL-U", "SHL-X", "SHL-PT", "REP", "HLP"):
        assert P[handle]["email"] not in paged, handle
    assert P["RES-P"]["email"] in set(verified_in_city("Pasig").values_list("email", flat=True))


def test_phones_are_verified_except_the_d9_gate_persona(settings):
    _run(settings)
    for handle in ("REP", "REP-C", "REP-T", "OWN", "FND", "HLP", "RES-A", "RES-B", "RES-P"):
        a = _acct(handle)
        assert a.phone and a.phone_verified_at, handle
    unverified = _acct("REP-U")
    assert unverified.phone is None and unverified.phone_verified_at is None


def test_standing_matches_the_plan(settings):
    _run(settings)
    assert _acct("RES-S").status == "suspended"
    assert _acct("SHL-V").shelter_profile.org_name == "Sagip Test Shelter"
    assert not account_is_verified_rescuer(_acct("SHL-U"))
    assert account_is_verified_rescuer(_acct("SHL-X"))
    assert _acct("RES-B").capabilities.filter(capability="rescuer", status="approved").exists()
    # ST-2 makes SHL-PT a partner during the run; the seed starts it as not one.
    assert ShelterProfile.objects.get(account=_acct("SHL-PT")).is_escalation_partner is False
    for handle in sagip_fixtures.DEFAULT_HANDLES:
        if P[handle].get("city"):
            assert list(Address.objects.filter(account=_acct(handle), is_primary=True)
                        .values_list("city", flat=True)) == [P[handle]["city"]], handle


def test_the_owner_has_bruno_with_two_photos_one_primary(settings):
    _run(settings)
    bruno = Pet.objects.get(owner_account=_acct("OWN"), name="Bruno")
    assert (bruno.species, bruno.breed, bruno.color_markings, bruno.size_category, bruno.sex) == (
        "dog", "Aspin", "brown, white chest", "medium", "male")
    assert bruno.photos.count() == 2
    assert bruno.photos.filter(is_primary=True).count() == 1


def test_a_rerun_resets_standing_without_duplicating_rows(settings):
    _run(settings)
    ids = {h: _acct(h).pk for h in sagip_fixtures.DEFAULT_HANDLES}
    rep = _acct("REP")
    rep.status = "suspended"
    rep.save(update_fields=["status"])
    ShelterProfile.objects.filter(account=_acct("SHL-PT")).update(is_escalation_partner=True)
    Address.objects.create(account=rep, city="Cebu City", is_primary=True)
    from verifications.models import VerificationRequest
    VerificationRequest.objects.create(account=_acct("SHL-U"), type="shelter_org",
                                       status="approved")

    _run(settings)

    assert {h: _acct(h).pk for h in sagip_fixtures.DEFAULT_HANDLES} == ids
    assert _acct("REP").status == "active"
    assert ShelterProfile.objects.get(account=_acct("SHL-PT")).is_escalation_partner is False
    assert not account_is_verified_rescuer(_acct("SHL-U"))
    assert list(Address.objects.filter(account=rep, is_primary=True)
                .values_list("city", flat=True)) == ["Marikina City"]
    assert Pet.objects.filter(owner_account=_acct("OWN"), name="Bruno").count() == 1
    assert Pet.objects.get(owner_account=_acct("OWN"), name="Bruno").photos.count() == 2


def test_accounts_missing_settings_are_backfilled(settings):
    bare = Account.objects.create(account_type="personal", email="bare@example.com",
                                  display_name="Bare")
    assert not AccountSettings.objects.filter(account=bare).exists()
    _run(settings)
    assert AccountSettings.objects.filter(account=bare).exists()


def test_staff_can_be_issued_a_token_without_a_password(settings):
    _run(settings)
    user = User.objects.get(username=sagip_fixtures.STAFF_USERNAME)
    assert user.is_staff and not user.has_usable_password()
    assert tokens_for_staff(user)["access"]
