"""T6 · a phone number changes in two phases (X7).

`POST /me/phone` used to write the unverified candidate straight onto the account, which did two
bad things at once: a person who started changing their number and walked away had LOST their
verified one (and with it the adoption-inquiry gate), and an unverified number sat in the
UNIQUE column, so anyone could block someone else's number from ever being used on Kupkop.

Now the candidate lives on the code row (`verification_code.target`). The account keeps its
current, verified number until the new one is PROVEN; only the verify step writes it.
"""
from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from common.otp import _hash
from verifications.models import VerificationCode

OLD = "+639171110000"
NEW = "+639172220000"
GOOD = "654321"


def _hdr(account):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(account)['access']}"}


def _verified(phone=OLD, **kw):
    return AccountFactory(email_verified_at=timezone.now(), phone=phone,
                          phone_verified_at=timezone.now() - timedelta(days=3), **kw)


def _request(client, account, phone):
    return client.post("/api/v1/me/phone", {"phone": phone}, content_type="application/json",
                       **_hdr(account))


def _verify(client, account, code=GOOD):
    return client.post("/api/v1/me/phone/verify", {"code": code}, content_type="application/json",
                       **_hdr(account))


def _know_the_code(account):
    """The real code is random and only the sender sees it: swap in a known one."""
    row = VerificationCode.objects.filter(account=account, purpose="phone",
                                          consumed_at__isnull=True).latest("created_at")
    row.code_hash = _hash(GOOD)
    row.save(update_fields=["code_hash"])
    return row


# ------------------------------------------------------------------- requesting a number

@pytest.mark.django_db
def test_requesting_a_new_number_leaves_the_verified_one_untouched(client):
    account = _verified()
    before = account.phone_verified_at

    res = _request(client, account, NEW)

    assert res.status_code == 202
    account.refresh_from_db()
    assert account.phone == OLD
    assert account.phone_verified_at == before
    assert VerificationCode.objects.get(account=account, purpose="phone").target == NEW


@pytest.mark.django_db
def test_a_first_number_is_not_written_until_it_is_verified(client):
    account = AccountFactory(email_verified_at=timezone.now())

    assert _request(client, account, "0917 222 0000").status_code == 202

    account.refresh_from_db()
    assert account.phone is None and account.phone_verified_at is None
    assert VerificationCode.objects.get(account=account, purpose="phone").target == NEW


@pytest.mark.django_db
def test_the_sms_goes_to_the_candidate_not_to_the_number_on_file(client):
    account = _verified()

    with mock.patch("common.otp.get_sender") as get_sender:
        _request(client, account, NEW)

    kwargs = get_sender.return_value.send.call_args.kwargs
    assert kwargs["channel"] == "sms" and kwargs["to"] == NEW


@pytest.mark.django_db
def test_an_abandoned_code_changes_nothing(client):
    account = _verified()
    _request(client, account, NEW)
    VerificationCode.objects.filter(account=account).update(
        expires_at=timezone.now() - timedelta(minutes=1))

    late = _verify(client, account)

    assert late.status_code == 410
    account.refresh_from_db()
    assert account.phone == OLD and account.phone_verified_at is not None


# --------------------------------------------------------------------------- verifying

@pytest.mark.django_db
def test_verifying_swaps_the_number_and_stamps_it(client):
    account = _verified()
    _request(client, account, NEW)
    _know_the_code(account)

    res = _verify(client, account)

    assert res.status_code == 200
    account.refresh_from_db()
    assert account.phone == NEW
    assert account.phone_verified_at > timezone.now() - timedelta(minutes=1)


@pytest.mark.django_db
def test_a_wrong_code_costs_an_attempt_and_changes_nothing(client):
    account = _verified()
    _request(client, account, NEW)
    _know_the_code(account)

    res = _verify(client, account, code="000000")

    assert res.status_code == 400
    account.refresh_from_db()
    assert account.phone == OLD


@pytest.mark.django_db
def test_re_verifying_the_number_already_on_file_works(client):
    account = _verified()
    _request(client, account, OLD)
    _know_the_code(account)

    assert _verify(client, account).status_code == 200

    account.refresh_from_db()
    assert account.phone == OLD and account.phone_verified_at > timezone.now() - timedelta(minutes=1)


@pytest.mark.django_db
def test_a_code_issued_by_an_older_build_still_verifies(client):
    # Rolling deploy: a code issued before this change has no `target` and the number is
    # already on the account. It must not strand the person mid-verification.
    account = AccountFactory(email_verified_at=timezone.now(), phone=NEW)
    VerificationCode.objects.create(
        account=account, channel="sms", purpose="phone", code_hash=_hash(GOOD),
        expires_at=timezone.now() + timedelta(minutes=5))

    res = _verify(client, account)

    assert res.status_code == 200
    account.refresh_from_db()
    assert account.phone == NEW and account.phone_verified_at is not None


# ------------------------------------------------------------------------- collisions

@pytest.mark.django_db
def test_a_number_someone_has_verified_is_a_generic_409_and_sends_no_code(client):
    AccountFactory(phone=NEW, phone_verified_at=timezone.now())
    account = _verified()

    res = _request(client, account, NEW)

    assert res.status_code == 409 and res.json()["error"]["code"] == "phone_taken"
    assert not VerificationCode.objects.filter(account=account, purpose="phone").exists()


@pytest.mark.django_db
def test_a_squatters_unverified_number_does_not_block_the_real_owner(client):
    # Legacy rows: an unverified number written by the old flow still sits in the UNIQUE column.
    squatter = AccountFactory(phone=NEW)                 # never verified
    account = _verified()

    assert _request(client, account, NEW).status_code == 202
    _know_the_code(account)
    res = _verify(client, account)

    assert res.status_code == 200
    account.refresh_from_db()
    squatter.refresh_from_db()
    assert account.phone == NEW
    assert squatter.phone is None          # never proven, so nothing is lost by evicting it


@pytest.mark.django_db
def test_someone_verifying_it_first_wins_and_the_candidate_is_discarded(client):
    account = _verified()
    _request(client, account, NEW)
    _know_the_code(account)
    AccountFactory(phone=NEW, phone_verified_at=timezone.now())      # they got there first

    res = _verify(client, account)

    assert res.status_code == 409 and res.json()["error"]["code"] == "phone_taken"
    account.refresh_from_db()
    assert account.phone == OLD
    # The code is spent: asking again must start over, not retry the lost number.
    assert _verify(client, account).status_code == 400


@pytest.mark.django_db
def test_the_collision_check_runs_on_the_normalised_number(client):
    AccountFactory(phone=NEW, phone_verified_at=timezone.now())
    account = _verified()

    res = _request(client, account, "0917 222 0000")

    assert res.status_code == 409
