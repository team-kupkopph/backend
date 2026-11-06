"""Review step 6 (MO5, MQ2) · actioning a shift or a donation QR flag now does something, and a flag whose
target is gone or has no surface can no longer be 'actioned' into a record that claims work nobody did.

Until now `resolve_flag(..., 'actioned')` on a shift or a QR set the flag to actioned and changed nothing:
a scam shift stayed open for signups while its flag left the queue."""
import uuid

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from moderation import targets
from moderation.actions import ModerationError, TargetUnavailable, resolve_flag
from moderation.models import FlagStatus, FlagTarget, ModerationFlag
from notifications.models import Notification
from shelter.models import DonationQr, QrStatus
from volunteer.models import ShiftStatus, SignupStatus, VolunteerShift, VolunteerSignup


def _flag(target_type, pk):
    return ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type=target_type,
                                         target_id=pk, reason="scam")


def _shift(owner=None, **kw):
    kw.setdefault("starts_at", timezone.now() + timezone.timedelta(days=2))
    kw.setdefault("ends_at", timezone.now() + timezone.timedelta(days=2, hours=2))
    return VolunteerShift.objects.create(shelter_account=owner or AccountFactory(account_type="shelter"),
                                         type="walking", capacity=5, status=ShiftStatus.OPEN, **kw)


def _signup(shift, status=SignupStatus.REQUESTED):
    return VolunteerSignup.objects.create(shift=shift, volunteer_account=AccountFactory(), status=status)


def _qr(status=QrStatus.LIVE, owner=None):
    return DonationQr.objects.create(account=owner or AccountFactory(account_type="shelter"), provider="gcash",
                                     account_name="PAWS Inc", qr_image_url="https://x/qr.png", status=status)


def _on_action(target_type, pk):
    return targets.describe_target(target_type, pk)["on_action"]


def _text(on_action):
    return " ".join(on_action["consequences"])


# ---- shifts -------------------------------------------------------------------------------------
@pytest.mark.django_db
def test_actioning_a_shift_flag_closes_the_shift_and_cancels_and_tells_its_signups():
    shift = _shift()
    people = [_signup(shift), _signup(shift, SignupStatus.APPROVED)]

    flag, effects = resolve_flag(_flag("shift", shift.pk), AccountFactory(), "actioned")

    shift.refresh_from_db()
    assert shift.status == ShiftStatus.CLOSED
    assert effects == {"shift_closed": str(shift.pk)} and flag.status == FlagStatus.ACTIONED
    for s in people:
        s.refresh_from_db()
        assert s.status == SignupStatus.CANCELLED
        assert Notification.objects.filter(account=s.volunteer_account, type="shift_cancelled_by_shelter").count() == 1


@pytest.mark.django_db
def test_the_shift_numbers_are_the_numbers_the_close_then_changes():
    shift = _shift()
    _signup(shift), _signup(shift, SignupStatus.APPROVED), _signup(shift, SignupStatus.DECLINED)
    out = _on_action(FlagTarget.SHIFT, shift.pk)
    resolve_flag(_flag("shift", shift.pk), AccountFactory(), "actioned")
    cancelled = VolunteerSignup.objects.filter(shift=shift, status=SignupStatus.CANCELLED, cancelled_by="platform").count()
    assert out["label"] == "Close shift" and out["changes"] is True
    assert cancelled == 2 and "2 signups will be cancelled" in _text(out)
    assert "can't be undone" in _text(out) and "shelter" in _text(out) and "not told" in _text(out)


@pytest.mark.django_db
def test_a_shift_with_no_signups_does_not_mention_signups():
    assert "signup" not in _text(_on_action(FlagTarget.SHIFT, _shift().pk)).replace("no more signups", "")


@pytest.mark.django_db
def test_a_closed_or_ended_shift_says_nothing_more_will_change_and_is_left_alone():
    closed = _shift()
    VolunteerShift.objects.filter(pk=closed.pk).update(status=ShiftStatus.CLOSED)
    ended = _shift(starts_at=timezone.now() - timezone.timedelta(days=2),
                   ends_at=timezone.now() - timezone.timedelta(days=2) + timezone.timedelta(hours=2))
    assert _on_action(FlagTarget.SHIFT, closed.pk)["already_done"] is True
    assert "Already closed" in _text(_on_action(FlagTarget.SHIFT, closed.pk))
    assert _on_action(FlagTarget.SHIFT, ended.pk)["already_done"] is True
    assert "already happened" in _text(_on_action(FlagTarget.SHIFT, ended.pk))
    for shift in (closed, ended):
        _, effects = resolve_flag(_flag("shift", shift.pk), AccountFactory(), "actioned")
        assert effects == {}
    ended.refresh_from_db()
    assert ended.status == ShiftStatus.OPEN          # an activity that already happened is never cancelled after the fact


# ---- donation QRs -------------------------------------------------------------------------------
@pytest.mark.django_db
def test_actioning_a_live_qr_flag_takes_it_down_and_tells_the_shelter():
    qr = _qr()
    reviewer = AccountFactory()

    _, effects = resolve_flag(_flag("qr", qr.pk), reviewer, "actioned")

    qr.refresh_from_db()
    assert qr.status == QrStatus.WITHDRAWN and qr.verified is False
    assert qr.decided_by_id == reviewer.pk and qr.decided_at is not None and qr.decision_note
    assert effects == {"qr_taken_down": str(qr.pk)}
    told = Notification.objects.get(account=qr.account, type="donation_qr_decided")
    assert told.data["decision"] == "taken_down"


@pytest.mark.django_db
def test_the_reporters_words_never_reach_the_shelter():
    """The decision note is stored and shown to the shelter. The reporter's free text must not be copied into it."""
    qr = _qr()
    flag = ModerationFlag.objects.create(reporter_account=AccountFactory(), target_type="qr", target_id=qr.pk,
                                         reason="this shelter is run by a thief called Marco")
    resolve_flag(flag, AccountFactory(), "actioned")
    qr.refresh_from_db()
    assert "Marco" not in qr.decision_note
    assert "Marco" not in Notification.objects.get(account=qr.account, type="donation_qr_decided").body


@pytest.mark.django_db
def test_the_qr_consequences_say_donors_lose_it_the_image_goes_and_the_shelter_is_told():
    out = _on_action(FlagTarget.QR, _qr().pk)
    text = _text(out)
    assert out["label"] == "Take down QR" and out["changes"] is True
    assert "Donors can no longer see" in text and "image is deleted" in text
    assert "shelter is told" in text and "upload a new one" in text


@pytest.mark.django_db
@pytest.mark.parametrize("status", [QrStatus.PENDING, QrStatus.REFUSED, QrStatus.WITHDRAWN])
def test_a_qr_that_is_not_live_has_nothing_to_take_down(status):
    qr = _qr(status)
    out = _on_action(FlagTarget.QR, qr.pk)
    assert out["already_done"] is True and out["changes"] is False and "isn't live" in _text(out)
    _, effects = resolve_flag(_flag("qr", qr.pk), AccountFactory(), "actioned")
    qr.refresh_from_db()
    assert effects == {} and qr.status == status
    assert not Notification.objects.filter(account=qr.account, type="donation_qr_decided").exists()


@pytest.mark.django_db
def test_an_account_flag_is_still_record_only():
    """MQ2 · an account flag never suspends: suspension is its own deliberate act."""
    owner = AccountFactory()
    out = _on_action(FlagTarget.ACCOUNT, owner.pk)
    assert out["label"] == "Mark handled" and out["changes"] is False
    assert resolve_flag(_flag("account", owner.pk), AccountFactory(), "actioned")[1] == {}


# ---- a target that is gone or has no surface ----------------------------------------------------
@pytest.mark.django_db
def test_a_flag_on_a_deleted_target_cannot_be_actioned_but_can_still_be_dismissed_or_reviewed():
    flag = _flag("listing", uuid.uuid4())
    with pytest.raises(TargetUnavailable) as err:
        resolve_flag(flag, AccountFactory(), "actioned")
    assert err.value.state == "gone" and isinstance(err.value, ModerationError)
    flag.refresh_from_db()
    assert flag.status == FlagStatus.OPEN and flag.reviewed_by is None

    for decision in ("reviewed", "dismissed"):
        again = _flag("listing", uuid.uuid4())
        assert resolve_flag(again, AccountFactory(), decision)[0].status == decision


@pytest.mark.django_db
def test_a_flag_on_a_type_with_no_surface_cannot_be_actioned():
    flag = _flag("message", uuid.uuid4())
    with pytest.raises(TargetUnavailable) as err:
        resolve_flag(flag, AccountFactory(), "actioned")
    assert err.value.state == "unsupported"
    assert resolve_flag(flag, AccountFactory(), "dismissed")[0].status == FlagStatus.DISMISSED


@pytest.mark.django_db
@pytest.mark.parametrize("target_type, state", [(FlagTarget.LISTING, "gone"), (FlagTarget.MESSAGE, "unsupported")])
def test_the_detail_says_there_is_nothing_to_action_instead_of_offering_a_button(target_type, state):
    out = targets.describe_target(target_type, uuid.uuid4())
    on_action = out["on_action"]
    assert out["state"] == state
    assert on_action["unavailable"] is True and on_action["label"] is None and on_action["changes"] is False
    assert "Dismiss" in _text(on_action) and "nothing to action" in _text(on_action)


@pytest.mark.django_db
def test_every_present_target_is_available():
    shift = _shift()
    assert _on_action(FlagTarget.SHIFT, shift.pk)["unavailable"] is False
