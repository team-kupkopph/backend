"""T8 · the shelter's own profile: read it, edit it, and what the public may see of it (AS7, X6, X13).

After onboarding a shelter could edit only its drop-off instructions and its vet. There was no
`GET /shelter/profile` (the Donation instructions screen read its own text back through the
PUBLIC needs route), no way to change the description, logo, official email or hours, and the
"Volunteers" and donations counts on its own profile were hard-coded to zero.

Two rules matter more than the happy path:

  * **The org name is what was verified.** Once any `shelter_org` verification is approved the
    name is locked (`409 org_name_locked`, "contact us to rename") so a verified shelter cannot
    quietly become a different organisation under the same badge.
  * **Contacts are never public.** A donor sees who the shelter is (name, description, logo,
    hours). The contact person, official email and phone and the vet are for the shelter's own
    eyes and the reviewers' — a public profile that leaked them would turn a feature into a
    harassment surface for the people running rescues.
"""
import json
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from community.models import NeedPledge, ShelterNeed
from shelter.models import ShelterProfile
from verifications.models import VerificationRequest
from volunteer.models import VolunteerShift, VolunteerSignup

CONTACTS = ("Ate Mia Santos", "mia.private@example.org", "+639175550101", "Dr. Vet Private",
            "1234567")


def _hdr(account):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(account)['access']}"}


def _shelter(*, verified=False, **profile):
    account = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    defaults = dict(org_name="Happy Paws", org_type="shelter", tier="community_rescue",
                    description="We rescue and rehome strays in Marikina.",
                    contact_person_name=CONTACTS[0], official_email=CONTACTS[1],
                    official_phone=CONTACTS[2], vet_name=CONTACTS[3], vet_prc_number=CONTACTS[4],
                    donation_instructions="Drop off at the gate, Sat 9-12.")
    defaults.update(profile)
    ShelterProfile.objects.create(account=account, **defaults)
    if verified:
        VerificationRequest.objects.create(account=account, type="shelter_org", status="approved")
    return account


def _get(client, account):
    return client.get("/api/v1/shelter/profile", **_hdr(account))


def _patch(client, account, body):
    return client.patch("/api/v1/shelter/profile", body, content_type="application/json",
                        **_hdr(account))


# ---------------------------------------------------------------------------------- read

@pytest.mark.django_db
def test_the_shelter_can_read_its_whole_profile(client):
    account = _shelter(operating_hours="Mon-Sat 9am-5pm", website_url="https://paws.example.org",
                       logo_url="https://bkt/shelter_logo/x/y.png")

    res = _get(client, account)

    assert res.status_code == 200
    body = res.json()
    assert body["org_name"] == "Happy Paws" and body["tier"] == "community_rescue"
    assert body["description"].startswith("We rescue")
    assert body["logo_url"] == "https://bkt/shelter_logo/x/y.png"
    assert body["operating_hours"] == "Mon-Sat 9am-5pm"
    assert body["official_email"] == CONTACTS[1] and body["official_phone"] == CONTACTS[2]
    assert body["contact_person_name"] == CONTACTS[0]
    assert body["vet_name"] == CONTACTS[3] and body["vet_prc_number"] == CONTACTS[4]
    assert body["donation_instructions"] == "Drop off at the gate, Sat 9-12."
    assert body["website_url"] == "https://paws.example.org"


@pytest.mark.django_db
def test_reading_before_setup_is_a_404_no_profile(client):
    account = AccountFactory(account_type="shelter", email_verified_at=timezone.now())

    res = _get(client, account)

    assert res.status_code == 404 and res.json()["error"]["code"] == "no_profile"


@pytest.mark.django_db
def test_a_personal_account_and_a_stranger_cannot_read_it(client):
    personal = AccountFactory(account_type="personal", email_verified_at=timezone.now())

    assert _get(client, personal).status_code == 403
    assert client.get("/api/v1/shelter/profile").status_code == 401


# -------------------------------------------------------------------------------- edit

@pytest.mark.django_db
def test_the_shelter_can_edit_description_hours_and_official_email(client):
    account = _shelter()

    res = _patch(client, account, {"description": "Now with a vet clinic.",
                                   "operating_hours": "Daily 8-6",
                                   "official_email": "hello@paws.example.org"})

    assert res.status_code == 200
    body = res.json()
    assert body["description"] == "Now with a vet clinic."
    assert body["operating_hours"] == "Daily 8-6"
    assert body["official_email"] == "hello@paws.example.org"
    stored = ShelterProfile.objects.get(account=account)
    assert stored.operating_hours == "Daily 8-6"


@pytest.mark.django_db
def test_an_invalid_official_email_is_a_field_error_and_blank_clears_it(client):
    account = _shelter()

    bad = _patch(client, account, {"official_email": "not-an-email"})
    cleared = _patch(client, account, {"official_email": ""})

    assert bad.status_code == 400
    assert "official_email" in json.dumps(bad.json())
    assert cleared.status_code == 200 and cleared.json()["official_email"] in ("", None)
    assert ShelterProfile.objects.get(account=account).official_email == ""


@pytest.mark.django_db
@pytest.mark.parametrize("field,value", [("description", "x" * 1001),
                                         ("operating_hours", "x" * 121),
                                         ("org_name", "x" * 151), ("org_name", "   ")])
def test_over_long_or_blank_values_are_refused(client, field, value):
    account = _shelter()

    assert _patch(client, account, {field: value}).status_code == 400


@pytest.mark.django_db
def test_tier_and_org_type_are_not_editable_here(client):
    # A tier change is an UPGRADE, and it re-runs verification (§3.5); it never goes through here.
    account = _shelter()

    res = _patch(client, account, {"tier": "registered_ngo", "org_type": "pound"})

    assert res.status_code == 200
    stored = ShelterProfile.objects.get(account=account)
    assert stored.tier == "community_rescue" and stored.org_type == "shelter"


@pytest.mark.django_db
def test_a_patch_before_setup_is_still_a_409_no_profile(client):
    account = AccountFactory(account_type="shelter", email_verified_at=timezone.now())

    assert _patch(client, account, {"description": "x"}).status_code == 409


# ------------------------------------------------------------------------- the org name

@pytest.mark.django_db
def test_an_unverified_shelter_may_rename_itself(client):
    account = _shelter()

    res = _patch(client, account, {"org_name": "Happier Paws"})

    assert res.status_code == 200 and res.json()["org_name"] == "Happier Paws"


@pytest.mark.django_db
def test_once_verified_the_name_is_locked(client):
    account = _shelter(verified=True)

    res = _patch(client, account, {"org_name": "A Different Organisation"})

    assert res.status_code == 409
    err = res.json()["error"]
    assert err["code"] == "org_name_locked" and "contact" in err["message"].lower()
    assert ShelterProfile.objects.get(account=account).org_name == "Happy Paws"


@pytest.mark.django_db
def test_sending_the_same_name_back_is_not_a_rename(client):
    # The edit screen submits the whole form; an unchanged name must not trip the lock.
    account = _shelter(verified=True)

    res = _patch(client, account, {"org_name": "Happy Paws", "description": "Updated."})

    assert res.status_code == 200 and res.json()["description"] == "Updated."


@pytest.mark.django_db
def test_a_pending_or_rejected_verification_does_not_lock_the_name(client):
    account = _shelter()
    VerificationRequest.objects.create(account=account, type="shelter_org", status="pending")
    VerificationRequest.objects.create(account=account, type="shelter_org", status="rejected")

    assert _patch(client, account, {"org_name": "Renamed"}).status_code == 200


@pytest.mark.django_db
def test_other_fields_stay_editable_after_verification(client):
    account = _shelter(verified=True)

    res = _patch(client, account, {"operating_hours": "Daily 8-6", "contact_person_name": "Ate Bea"})

    assert res.status_code == 200 and res.json()["contact_person_name"] == "Ate Bea"


# ------------------------------------------------------------------------------- logo

def _logo(account, purpose="shelter_logo", owner=None):
    return f"https://bkt.example/{purpose}/{(owner or account).pk}/{uuid.uuid4()}"


@pytest.mark.django_db
def test_the_logo_must_be_this_accounts_own_upload(client):
    account, other = _shelter(), _shelter()

    ok = _patch(client, account, {"logo_file_url": _logo(account)})
    theirs = _patch(client, account, {"logo_file_url": _logo(account, owner=other)})
    wrong_purpose = _patch(client, account, {"logo_file_url": _logo(account, purpose="donation_qr")})
    junk = _patch(client, account, {"logo_file_url": "javascript:alert(1)"})

    assert ok.status_code == 200 and ok.json()["logo_url"].startswith("https://bkt.example/shelter_logo/")
    assert theirs.status_code == 400 and wrong_purpose.status_code == 400 and junk.status_code == 400


@pytest.mark.django_db
def test_a_blank_logo_clears_it(client):
    account = _shelter(logo_url="https://bkt.example/shelter_logo/x/y")

    res = _patch(client, account, {"logo_file_url": ""})

    assert res.status_code == 200
    assert ShelterProfile.objects.get(account=account).logo_url == ""


@pytest.mark.django_db
def test_shelter_logo_is_a_public_upload_purpose(client):
    account = _shelter()

    res = client.post("/api/v1/media/presign", {"purpose": "shelter_logo", "content_type": "image/png"},
                      content_type="application/json", **_hdr(account))

    assert res.status_code == 200, res.content
    assert f"shelter_logo/{account.pk}/" in json.dumps(res.json())


# -------------------------------------------------------------------- dashboard counts

def _shift(shelter, **kw):
    start = timezone.now() + timedelta(days=2)
    return VolunteerShift.objects.create(shelter_account=shelter, type="feeding", starts_at=start,
                                         ends_at=start + timedelta(hours=2), capacity=5, **kw)


def _dashboard(client, account):
    return client.get("/api/v1/shelter/dashboard", **_hdr(account)).json()["counts"]


@pytest.mark.django_db
def test_volunteers_counts_each_person_once_and_only_those_who_took_part(client):
    shelter, other = _shelter(verified=True), _shelter(verified=True)
    vic, bea, cam, dee = (AccountFactory() for _ in range(4))
    first, second = _shift(shelter), _shift(shelter)
    VolunteerSignup.objects.create(shift=first, volunteer_account=vic, status="approved")
    VolunteerSignup.objects.create(shift=second, volunteer_account=vic, status="completed")  # same person
    VolunteerSignup.objects.create(shift=first, volunteer_account=bea, status="completed")
    VolunteerSignup.objects.create(shift=first, volunteer_account=cam, status="requested")
    VolunteerSignup.objects.create(shift=first, volunteer_account=dee, status="no_show")
    VolunteerSignup.objects.create(shift=_shift(other), volunteer_account=AccountFactory(),
                                   status="approved")                               # not theirs

    assert _dashboard(client, shelter)["volunteers"] == 2        # vic once, bea; not cam / dee / other


@pytest.mark.django_db
def test_donations_counts_pledges_that_actually_arrived(client):
    shelter, other = _shelter(verified=True), _shelter(verified=True)
    need = ShelterNeed.objects.create(shelter_account=shelter, title="Dog food", category="food",
                                      quantity_needed=10)
    theirs = ShelterNeed.objects.create(shelter_account=other, title="Cat litter", category="food",
                                        quantity_needed=10)
    for status in ("delivered", "delivered", "pledged", "cancelled"):
        NeedPledge.objects.create(need=need, pledger_account=AccountFactory(), quantity=2,
                                  status=status)
    NeedPledge.objects.create(need=theirs, pledger_account=AccountFactory(), quantity=2,
                              status="delivered")

    assert _dashboard(client, shelter)["donations"] == 2


@pytest.mark.django_db
def test_a_shelter_with_nothing_yet_has_zero_not_an_error(client):
    counts = _dashboard(client, _shelter(verified=True))

    assert counts["volunteers"] == 0 and counts["donations"] == 0


# ------------------------------------------------------------ what the public may see

def _live_shelter():
    return _shelter(verified=True, operating_hours="Mon-Sat 9am-5pm",
                    logo_url="https://bkt.example/shelter_logo/a/b")


@pytest.mark.django_db
def test_a_donor_sees_who_the_shelter_is(client):
    shelter = _live_shelter()

    body = client.get(f"/api/v1/shelters/{shelter.pk}/needs").json()

    assert body["shelter"] == {"org_name": "Happy Paws",
                               "description": "We rescue and rehome strays in Marikina.",
                               "logo_url": "https://bkt.example/shelter_logo/a/b",
                               "operating_hours": "Mon-Sat 9am-5pm"}


@pytest.mark.django_db
def test_no_contact_detail_is_ever_public(client):
    shelter = _live_shelter()
    ShelterNeed.objects.create(shelter_account=shelter, title="Dog food", category="food",
                               quantity_needed=10)

    public = json.dumps(client.get(f"/api/v1/shelters/{shelter.pk}/needs").json())
    discovery = json.dumps(client.get("/api/v1/shelters/ways-to-help").json())

    assert "Happy Paws" in public and "Happy Paws" in discovery      # the scan sees the shelter
    for contact in CONTACTS:
        assert contact not in public, f"{contact!r} leaked on the public wishlist"
        assert contact not in discovery, f"{contact!r} leaked on ways-to-help"


@pytest.mark.django_db
def test_an_unverified_shelter_shows_nothing_to_the_public_but_everything_to_its_owner(client):
    shelter = _shelter()                                  # never verified: not live

    public = client.get(f"/api/v1/shelters/{shelter.pk}/needs").json()
    own = client.get(f"/api/v1/shelters/{shelter.pk}/needs", **_hdr(shelter)).json()

    assert public["shelter"] is None
    assert own["shelter"]["org_name"] == "Happy Paws"
