from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.factories import AccountFactory
from accounts.models import Address
from volunteer.models import ShiftStatus, SignupStatus, VolunteerShift, VolunteerSignup


def _c(account):
    c = APIClient(); c.force_authenticate(user=account); return c


def _shift(shelter):
    now = timezone.now()
    return VolunteerShift.objects.create(shelter_account=shelter, type="walking",
        starts_at=now + timedelta(days=2), ends_at=now + timedelta(days=2, hours=2),
        capacity=4, status=ShiftStatus.OPEN)


def _signup(shelter, vol, *, consent, status=SignupStatus.APPROVED):
    return VolunteerSignup.objects.create(shift=_shift(shelter), volunteer_account=vol,
        status=status, contact_share_consent=consent,
        contact_share_consent_at=timezone.now() if consent else None)


@pytest.mark.django_db
def test_contact_present_when_consented_and_live():
    shelter = AccountFactory(account_type="shelter"); vol = AccountFactory(account_type="personal",
        phone="+639170000001", email="vol1@example.com")
    Address.objects.create(account=vol, city="Quezon City", line1="1 Main St", is_primary=True)
    su = _signup(shelter, vol, consent=True)
    body = _c(shelter).get(f"/api/v1/shelter/signups/{su.pk}/volunteer").json()
    assert body["display_name"] == vol.display_name
    assert body["contact"]["phone"] == "+639170000001"
    assert body["contact"]["email"] == "vol1@example.com"
    assert body["contact"]["address"]["city"] == "Quezon City"
    assert set(body["reliability"]) == {"shifts_completed","no_shows","consecutive_no_shows",
                                        "needs_reapproval","is_reliable"}


@pytest.mark.django_db
def test_contact_absent_not_null_without_consent():
    shelter = AccountFactory(account_type="shelter"); vol = AccountFactory(account_type="personal")
    su = _signup(shelter, vol, consent=False)
    body = _c(shelter).get(f"/api/v1/shelter/signups/{su.pk}/volunteer").json()
    assert "contact" not in body            # absent, not null
    assert body["display_name"] == vol.display_name


@pytest.mark.django_db
@pytest.mark.parametrize("terminal", [SignupStatus.CANCELLED, SignupStatus.DECLINED])
def test_contact_revoked_on_terminal_status(terminal):
    shelter = AccountFactory(account_type="shelter"); vol = AccountFactory(account_type="personal",
        phone="+639170000002")
    su = _signup(shelter, vol, consent=True, status=terminal)
    body = _c(shelter).get(f"/api/v1/shelter/signups/{su.pk}/volunteer").json()
    assert "contact" not in body            # terminal ends the §12.5 exception


@pytest.mark.django_db
def test_per_shift_isolation():
    # Consent on shift A must not reveal contact on shift B (a separate no-consent signup).
    shelter = AccountFactory(account_type="shelter"); vol = AccountFactory(account_type="personal",
        phone="+639170000003")
    _signup(shelter, vol, consent=True)                       # shift A, consented
    su_b = _signup(shelter, vol, consent=False)               # shift B, not consented
    body = _c(shelter).get(f"/api/v1/shelter/signups/{su_b.pk}/volunteer").json()
    assert "contact" not in body


@pytest.mark.django_db
def test_foreign_shelter_403_and_guest_401():
    shelter = AccountFactory(account_type="shelter"); other = AccountFactory(account_type="shelter")
    vol = AccountFactory(account_type="personal")
    su = _signup(shelter, vol, consent=True)
    assert _c(other).get(f"/api/v1/shelter/signups/{su.pk}/volunteer").status_code == 403
    assert APIClient().get(f"/api/v1/shelter/signups/{su.pk}/volunteer").status_code == 401


@pytest.mark.django_db
def test_no_other_account_contact_leaks():
    import json
    shelter = AccountFactory(account_type="shelter"); vol = AccountFactory(account_type="personal")
    _noise = AccountFactory(account_type="personal", phone="+639175550000", email="noise@example.com")
    su = _signup(shelter, vol, consent=False)
    dumped = json.dumps(_c(shelter).get(f"/api/v1/shelter/signups/{su.pk}/volunteer").json())
    assert "+639175550000" not in dumped and "noise@example.com" not in dumped


@pytest.mark.django_db
def test_shelter_shift_get_owner_only():
    shelter = AccountFactory(account_type="shelter"); other = AccountFactory(account_type="shelter")
    s = _shift(shelter)
    ok = _c(shelter).get(f"/api/v1/shelter/shifts/{s.pk}").json()
    assert ok["shift_id"] == str(s.pk) and ok["org_name"] == shelter.display_name
    assert _c(other).get(f"/api/v1/shelter/shifts/{s.pk}").status_code == 403
