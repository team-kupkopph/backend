"""US-N1 · DELETE /me — the RA 10173 erasure right (§12.6, §12.7).

D-S7-1: this is a SOFT delete, and the purge that follows anonymises in place. Row deletion
is not merely undesirable, it is refused by the database — `rescue_case.claimed_by_account`,
`adoption_inquiry.adopter_account` and `volunteer_signup.volunteer_account` are all PROTECT,
so a hard delete raises rather than cascading. §12.7 independently prefers the same thing:
public content is reassigned to a "deleted user" rather than orphaned.

Two properties matter more than the happy path:

  * **Commitments are other people's plans.** An open rescue claim means an animal is waiting
    on this person; an approved shift means a shelter has rostered them. Deletion must refuse
    and say what to close, the same posture as the binding claim everywhere else in Sagip.
  * **Deletion must not become an enumeration oracle.** Login on a deleted account returns
    the same generic 401 as a wrong password (§12.1), never "this account was deleted".
"""
import pytest
from django.contrib.gis.geos import Point
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.models import Account, AccountStatus
from accounts.tokens import tokens_for


def _auth(account):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(account)['access']}"}


def _report(**kw):
    from sagip.models import StrayReport
    defaults = dict(species="dog", condition="injured", geom=Point(121.05, 14.63, srid=4326))
    defaults.update(kw)
    return StrayReport.objects.create(**defaults)


def _listing(poster, **kw):
    """A listing that is actually visible publicly — which needs its poster to satisfy
    `public_poster_q()` (an approved `rescuer` capability). Without it the browse hides the
    row for a reason unrelated to deletion, and the test would pass for the wrong reason."""
    from listings.models import AdoptionListing
    from verifications.models import AccountCapability
    AccountCapability.objects.get_or_create(account=poster, capability="rescuer",
                                            defaults={"status": "approved"})
    defaults = dict(name="Bantay", species="dog", city="Marikina", adoption_fee="300.00")
    defaults.update(kw)
    return AdoptionListing.objects.create(posted_by=poster, **defaults)


def _delete(client, account, **kw):
    return client.delete("/api/v1/me", {"confirm": True},
                         content_type="application/json", **_auth(account), **kw)


# ---------------------------------------------------------------- the happy path

@pytest.mark.django_db
def test_deleting_flips_status_and_opens_the_grace_window(client):
    account = AccountFactory()
    res = _delete(client, account)

    assert res.status_code == 204
    account.refresh_from_db()
    assert account.status == AccountStatus.DELETED
    assert account.deleted_at is not None          # the M5 pair, enforced by the DB
    assert account.anonymized_at is None           # nothing is scrubbed until the sweep


@pytest.mark.django_db
def test_every_existing_session_dies_immediately(client):
    # §12.7: "sessions revoked immediately" — not at next login, not when the token expires.
    account = AccountFactory()
    auth = _auth(account)
    assert client.get("/api/v1/me", **auth).status_code == 200

    client.delete("/api/v1/me", {"confirm": True}, content_type="application/json", **auth)

    assert client.get("/api/v1/me", **auth).status_code == 401


@pytest.mark.django_db
def test_device_tokens_are_removed_so_a_deleted_account_stops_being_pushed(client):
    from devices.models import DeviceToken
    account = AccountFactory()
    DeviceToken.objects.create(account=account, fcm_token="tok-1", platform="ios")

    _delete(client, account)

    assert not DeviceToken.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_deleting_twice_is_idempotent_and_does_not_restart_the_grace_window(client):
    account = AccountFactory()
    _delete(client, account)
    account.refresh_from_db()
    first_deleted_at = account.deleted_at

    # The account's tokens are dead, so a second attempt comes from a fresh login path;
    # simulate it directly against the service to prove the state does not move.
    from accounts.lifecycle import soft_delete_account
    soft_delete_account(account)

    account.refresh_from_db()
    assert account.deleted_at == first_deleted_at


@pytest.mark.django_db
def test_confirmation_is_required(client):
    # A DELETE with no body must not erase an account because a client sent it by accident.
    account = AccountFactory()
    res = client.delete("/api/v1/me", {}, content_type="application/json", **_auth(account))

    assert res.status_code == 400
    account.refresh_from_db()
    assert account.status == AccountStatus.ACTIVE


# ---------------------------------------------------------------- enumeration safety

@pytest.mark.django_db
def test_login_on_a_deleted_account_is_generically_refused(client):
    account = AccountFactory(email="gone@kupkop.ph")
    account.set_password("correct-horse")
    account.email_verified_at = timezone.now()
    account.save()
    _delete(client, account)

    res = client.post("/api/v1/auth/login", {"email": "gone@kupkop.ph",
                                             "password": "correct-horse"},
                      content_type="application/json")

    assert res.status_code == 401
    # §12.1 · byte-identical to a wrong password. "account_deleted" here would tell an
    # attacker the address is real, which is exactly what the generic message prevents.
    assert res.json()["error"]["code"] == "invalid_credentials"
    assert "delet" not in res.json()["error"]["message"].lower()


@pytest.mark.django_db
def test_signing_up_again_with_a_deleted_address_is_refused_cleanly_not_with_a_500(client):
    """The row survives by design and `email` is UNIQUE, so this could have been a
    unique-constraint 500. It is not: signup's existing duplicate branch already returns a
    clean 409, and it is deliberately left saying `email_taken` rather than gaining an
    `account_deleted` code — a distinct code would tell an attacker not just that the
    address exists (which §12.1 already permits signup to reveal) but that it was deleted,
    for no benefit a returning user can act on during the grace window.

    ⚠️ OPEN PRODUCT EDGE (flagged in the plan, not decided here): after anonymisation the
    scrubbed address is freed, so the same person CAN sign up again then. Whether they
    should be able to *restore* during the 30 days is a product decision nobody has made.
    """
    account = AccountFactory(email="returning@kupkop.ph")
    _delete(client, account)

    res = client.post("/api/v1/auth/signup",
                      {"email": "returning@kupkop.ph", "password": "Str0ng!passw0rd",
                       "display_name": "Back Again", "account_type": "personal",
                       "terms_consent": True},
                      content_type="application/json")

    assert res.status_code == 409
    assert res.json()["error"]["code"] == "email_taken"


# ---------------------------------------------------------------- open commitments

@pytest.mark.django_db
def test_an_open_rescue_claim_blocks_deletion(client):
    from sagip.models import RescueCase
    account = AccountFactory()
    RescueCase.objects.create(report=_report(), claimed_by_account=account)

    res = _delete(client, account)

    assert res.status_code == 409
    body = res.json()["error"]
    assert body["code"] == "has_active_commitments"
    # The list is the point — "you have commitments" with no detail is a dead end.
    assert any(b["kind"] == "rescue_claim" for b in body["details"]["blockers"])
    assert Account.objects.get(pk=account.pk).status == AccountStatus.ACTIVE


@pytest.mark.django_db
def test_a_resolved_claim_does_not_block(client):
    from sagip.models import RescueCase
    account = AccountFactory()
    RescueCase.objects.create(report=_report(), claimed_by_account=account,
                              resolved_at=timezone.now())

    assert _delete(client, account).status_code == 204


@pytest.mark.django_db
def test_an_upcoming_approved_shift_blocks_deletion(client):
    from datetime import timedelta

    from volunteer.models import SignupStatus, VolunteerShift, VolunteerSignup
    volunteer, shelter = AccountFactory(), AccountFactory()
    shift = VolunteerShift.objects.create(
        shelter_account=shelter, starts_at=timezone.now() + timedelta(days=3),
        ends_at=timezone.now() + timedelta(days=3, hours=2), capacity=4)
    VolunteerSignup.objects.create(shift=shift, volunteer_account=volunteer,
                                   status=SignupStatus.APPROVED)

    res = _delete(client, volunteer)

    assert res.status_code == 409
    assert any(b["kind"] == "volunteer_shift"
               for b in res.json()["error"]["details"]["blockers"])


@pytest.mark.django_db
def test_a_past_shift_does_not_block(client):
    from datetime import timedelta

    from volunteer.models import SignupStatus, VolunteerShift, VolunteerSignup
    volunteer, shelter = AccountFactory(), AccountFactory()
    shift = VolunteerShift.objects.create(
        shelter_account=shelter, starts_at=timezone.now() - timedelta(days=3),
        ends_at=timezone.now() - timedelta(days=3, hours=-2), capacity=4)
    VolunteerSignup.objects.create(shift=shift, volunteer_account=volunteer,
                                   status=SignupStatus.COMPLETED)

    assert _delete(client, volunteer).status_code == 204


@pytest.mark.django_db
def test_an_active_adoption_inquiry_blocks_the_adopter(client):
    from listings.models import AdoptionInquiry, InquiryStatus
    adopter = AccountFactory()
    AdoptionInquiry.objects.create(listing=_listing(AccountFactory()), adopter_account=adopter,
                                   status=InquiryStatus.ACTIVE)

    res = _delete(client, adopter)

    assert res.status_code == 409
    assert any(b["kind"] == "adoption_inquiry"
               for b in res.json()["error"]["details"]["blockers"])


@pytest.mark.django_db
def test_a_poster_with_someone_elses_active_inquiry_is_blocked_too(client):
    # The person waiting on an answer is the one who would be stranded.
    from listings.models import AdoptionInquiry, InquiryStatus
    poster = AccountFactory()
    listing = _listing(poster)
    AdoptionInquiry.objects.create(listing=listing, adopter_account=AccountFactory(),
                                   status=InquiryStatus.ACTIVE)

    res = _delete(client, poster)

    assert res.status_code == 409
    assert any(b["kind"] == "adoption_inquiry"
               for b in res.json()["error"]["details"]["blockers"])


@pytest.mark.django_db
def test_blockers_are_listed_together_not_one_at_a_time(client):
    # Fixing one and being told about the next is the worst version of this screen.
    from datetime import timedelta

    from sagip.models import RescueCase
    from volunteer.models import SignupStatus, VolunteerShift, VolunteerSignup
    account = AccountFactory()
    RescueCase.objects.create(report=_report(), claimed_by_account=account)
    shift = VolunteerShift.objects.create(
        shelter_account=AccountFactory(), starts_at=timezone.now() + timedelta(days=2),
        ends_at=timezone.now() + timedelta(days=2, hours=2), capacity=4)
    VolunteerSignup.objects.create(shift=shift, volunteer_account=account,
                                   status=SignupStatus.REQUESTED)

    kinds = {b["kind"] for b in _delete(client, account).json()["error"]["details"]["blockers"]}
    assert kinds == {"rescue_claim", "volunteer_shift"}


# ---------------------------------------------------------------- public invisibility

@pytest.mark.django_db
def test_a_deleted_accounts_listing_leaves_the_public_browse(client):
    poster = AccountFactory()
    _listing(poster)
    assert len(client.get("/api/v1/listings").json()["results"]) == 1

    _delete(client, poster)

    assert client.get("/api/v1/listings").json()["results"] == []


@pytest.mark.django_db
def test_a_deleted_accounts_story_leaves_the_feed(client):
    from community.models import StoryPost
    author = AccountFactory()
    StoryPost.objects.create(author_account=author, story_type="general", caption="hello")
    assert len(client.get("/api/v1/stories").json()["results"]) == 1

    _delete(client, author)

    assert client.get("/api/v1/stories").json()["results"] == []
