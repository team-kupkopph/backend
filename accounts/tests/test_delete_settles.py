"""T4 · leaving settles the other side (AS3, AS4, X4).

Closing a need already tells its donors and cancels their pledges (WD1). Deleting an account is
the same act from the other end, and it used to do none of it: a deleted shelter's donors kept a
"Pledged" card for a shelter that was gone, and a deleted donor left the shelter planning around
goods that would never arrive.

  * A leaving SHELTER closes its open needs through the same writer Mark complete uses, so
    every waiting donor is told and every pledge ends exactly one way; its QR goes dark.
  * A leaving DONOR's waiting pledges are cancelled (`donor_left`) and the shelter is told —
    WITHOUT the donor's name, because the notification outlives the erasure.
  * All of it happens in the delete's own transaction: a failure leaves the account active.
"""
from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.lifecycle import soft_delete_account
from accounts.models import AccountStatus
from community.models import NeedPledge, NeedStatus, PledgeStatus, ShelterNeed
from notifications.models import Notification
from shelter.models import DonationQr, QrStatus


def _shelter(**kw):
    return AccountFactory(account_type="shelter", display_name="Happy Paws", **kw)


def _need(shelter, *, needed=10, received=0, status="open", title="Dog food"):
    return ShelterNeed.objects.create(shelter_account=shelter, title=title, category="food",
                                      quantity_needed=needed, quantity_received=received,
                                      status=status)


def _pledge(need, donor=None, *, quantity=3, status="pledged", **kw):
    return NeedPledge.objects.create(need=need, pledger_account=donor or AccountFactory(),
                                     quantity=quantity, status=status, **kw)


def _notes(account, type_):
    return list(Notification.objects.filter(account=account, type=type_))


# ------------------------------------------------------------------ a leaving shelter

@pytest.mark.django_db
def test_a_leaving_shelter_closes_its_open_needs_and_cancels_the_waiting_pledges():
    shelter = _shelter()
    need = _need(shelter, needed=10, received=2)
    donors = [AccountFactory(display_name="Donor A"), AccountFactory(display_name="Donor B")]
    pledges = [_pledge(need, d) for d in donors]

    soft_delete_account(shelter)

    need.refresh_from_db()
    assert need.status == NeedStatus.CLOSED
    for p in pledges:
        p.refresh_from_db()
        assert p.status == PledgeStatus.CANCELLED and p.cancel_reason == "need_closed"
        assert p.decided_at is not None


@pytest.mark.django_db
def test_every_waiting_donor_is_told_that_the_shelter_left():
    shelter = _shelter()
    need = _need(shelter, title="Cat litter")
    donor = AccountFactory()
    pledge = _pledge(need, donor)

    soft_delete_account(shelter)

    (note,) = _notes(donor, "need_closed")
    assert "Happy Paws" in note.body and "left Kupkop" in note.body
    assert "Cat litter" in note.body
    assert note.data == {"need_id": str(need.pk), "pledge_id": str(pledge.pk)}


@pytest.mark.django_db
def test_a_need_that_already_met_its_target_stays_fulfilled_and_its_late_pledges_settle():
    shelter = _shelter()
    need = _need(shelter, needed=5, received=5, status="fulfilled")
    donor = AccountFactory()
    pledge = _pledge(need, donor)               # an over-pledge still waiting (R1)

    soft_delete_account(shelter)

    need.refresh_from_db()
    pledge.refresh_from_db()
    assert need.status == NeedStatus.FULFILLED
    assert pledge.status == PledgeStatus.CANCELLED
    assert len(_notes(donor, "need_closed")) == 1


@pytest.mark.django_db
def test_delivered_pledges_and_closed_needs_are_facts_and_stay():
    shelter = _shelter()
    open_need = _need(shelter, received=3)
    delivered = _pledge(open_need, status="delivered", quantity_received=3)
    closed_need = _need(shelter, status="closed", title="Old drive")
    already_cancelled = _pledge(closed_need, status="cancelled", cancel_reason="need_closed")

    soft_delete_account(shelter)

    delivered.refresh_from_db()
    already_cancelled.refresh_from_db()
    closed_need.refresh_from_db()
    assert delivered.status == PledgeStatus.DELIVERED
    assert already_cancelled.status == PledgeStatus.CANCELLED
    assert closed_need.status == NeedStatus.CLOSED
    assert Notification.objects.filter(type="need_closed").count() == 0


@pytest.mark.django_db
def test_a_leaving_shelters_qr_goes_dark():
    shelter = _shelter()
    live = DonationQr.objects.create(account=shelter, provider="gcash", account_name="Happy Paws",
                                     qr_image_url="https://bkt/a.png", status=QrStatus.LIVE)
    pending = DonationQr.objects.create(account=shelter, provider="maya", account_name="Happy Paws",
                                        qr_image_url="https://bkt/b.png", status=QrStatus.PENDING)
    refused = DonationQr.objects.create(account=shelter, provider="maya", account_name="Happy Paws",
                                        qr_image_url="https://bkt/c.png", status=QrStatus.REFUSED)

    soft_delete_account(shelter)

    for qr, expected in ((live, QrStatus.WITHDRAWN), (pending, QrStatus.WITHDRAWN),
                         (refused, QrStatus.REFUSED)):          # a decided row is the audit trail
        qr.refresh_from_db()
        assert qr.status == expected
    live.refresh_from_db()
    assert live.verified is False               # the stored mirror follows the status


# --------------------------------------------------------------------- a leaving donor

@pytest.mark.django_db
def test_a_leaving_donors_waiting_pledges_are_cancelled_as_donor_left():
    donor = AccountFactory(display_name="Dani Donor")
    shelter = _shelter()
    need = _need(shelter)
    waiting = _pledge(need, donor)
    delivered = _pledge(need, donor, status="delivered", quantity_received=2)
    someone_elses = _pledge(need, AccountFactory())

    soft_delete_account(donor)

    for p in (waiting, delivered, someone_elses):
        p.refresh_from_db()
    assert waiting.status == PledgeStatus.CANCELLED and waiting.cancel_reason == "donor_left"
    assert waiting.decided_at is not None
    assert delivered.status == PledgeStatus.DELIVERED          # a recorded fact
    assert someone_elses.status == PledgeStatus.PLEDGED        # not theirs to touch


@pytest.mark.django_db
def test_the_shelter_is_told_without_the_donors_name():
    donor = AccountFactory(display_name="Dani Donor", email="dani.private@example.com")
    shelter = _shelter()
    need = _need(shelter, title="Dog food")
    pledge = _pledge(need, donor)

    soft_delete_account(donor)

    (note,) = _notes(shelter, "pledge_cancelled")
    assert "Dog food" in note.body
    # The notification outlives the erasure: it must not be where the name survives.
    assert "Dani" not in note.body and "Dani" not in note.title
    assert "dani.private@example.com" not in note.body
    assert note.data == {"need_id": str(need.pk), "pledge_id": str(pledge.pk)}


@pytest.mark.django_db
def test_nothing_is_sent_to_the_account_that_is_leaving():
    donor = AccountFactory()
    _pledge(_need(_shelter()), donor)

    soft_delete_account(donor)

    assert not Notification.objects.filter(
        account=donor, type__in=["need_closed", "pledge_cancelled"]).exists()


@pytest.mark.django_db
def test_an_account_that_is_both_settles_both_ways():
    both = _shelter()
    other_shelter = _shelter()
    own_need = _need(both)
    own_pledge = _pledge(own_need)                          # someone pledged to them
    their_pledge = _pledge(_need(other_shelter), both)      # and they pledged elsewhere

    soft_delete_account(both)

    own_pledge.refresh_from_db()
    their_pledge.refresh_from_db()
    assert own_pledge.cancel_reason == "need_closed"
    assert their_pledge.cancel_reason == "donor_left"


# ----------------------------------------------------------------- transaction + repeats

@pytest.mark.django_db
def test_a_failure_while_settling_leaves_the_account_active(monkeypatch):
    shelter = _shelter()
    need = _need(shelter)
    _pledge(need)

    def boom(*args, **kwargs):
        raise RuntimeError("settlement failed")

    monkeypatch.setattr("community.needs.complete_need", boom)
    with pytest.raises(RuntimeError):
        soft_delete_account(shelter)

    shelter.refresh_from_db()
    assert shelter.status == AccountStatus.ACTIVE and shelter.deleted_at is None
    need.refresh_from_db()
    assert need.status == NeedStatus.OPEN


@pytest.mark.django_db
def test_a_second_delete_settles_and_tells_nobody_twice():
    shelter = _shelter()
    donor = AccountFactory()
    _pledge(_need(shelter), donor)

    soft_delete_account(shelter)
    soft_delete_account(shelter)

    assert len(_notes(donor, "need_closed")) == 1


@pytest.mark.django_db
def test_restoring_does_not_undo_what_leaving_settled(client):
    shelter = _shelter(email_verified_at=timezone.now(), password="Right1234")
    need = _need(shelter)
    pledge = _pledge(need)
    qr = DonationQr.objects.create(account=shelter, provider="gcash", account_name="Happy Paws",
                                   qr_image_url="https://bkt/a.png", status=QrStatus.LIVE)
    soft_delete_account(shelter)
    # Backdate so the restored session's iat is after the revocation (second-granular check).
    shelter.sessions_revoked_at = timezone.now() - timedelta(days=1)
    shelter.save(update_fields=["sessions_revoked_at"])

    challenge = client.post("/api/v1/auth/login", {"email": shelter.email, "password": "Right1234"},
                            content_type="application/json")
    token = challenge.json()["error"]["details"]["restore_token"]
    restored = client.post("/api/v1/auth/restore", {"restore_token": token},
                           content_type="application/json")

    assert restored.status_code == 200
    for obj in (shelter, need, pledge, qr):
        obj.refresh_from_db()
    assert shelter.status == AccountStatus.ACTIVE
    # The donors were told their pledges ended; quietly resurrecting them would contradict that.
    assert pledge.status == PledgeStatus.CANCELLED
    assert need.status == NeedStatus.CLOSED
    assert qr.status == QrStatus.WITHDRAWN


# ------------------------------------------------------------------------ the constraint

@pytest.mark.django_db
def test_donor_left_is_an_allowed_cancel_reason_and_a_bogus_one_still_is_not():
    need = _need(_shelter())
    ok = _pledge(need, status="cancelled", cancel_reason="donor_left")
    assert ok.cancel_reason == "donor_left"

    with pytest.raises(IntegrityError), transaction.atomic():
        _pledge(need, status="cancelled", cancel_reason="because")
