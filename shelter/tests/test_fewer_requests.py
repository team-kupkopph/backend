"""SH12 (shelter home review, 2026-10-29) · step 15, fewer requests on focus.

- Home (owner and shelter) fetched the WHOLE `/me/notifications` list on every focus only to draw the bell's dot.
  `GET /me/notifications/unread-count` answers that with one count.
- You fetched page 1 of `/shelter/shifts?when=upcoming` only to count the open and full ones, and showed "20+"
  past a page. The dashboard it already loads now carries `volunteer.active_shifts`: the same set (not ended,
  open or full), counted in SQL.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from notifications.models import Notification
from volunteer.models import ShiftStatus, VolunteerShift


def _hdr(acc):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(acc)['access']}"}


@pytest.mark.django_db
class TestUnreadCount:
    URL = "/api/v1/me/notifications/unread-count"

    def test_counts_only_the_callers_unread(self, client):
        me, other = AccountFactory(), AccountFactory()
        for read in (False, False, True):
            Notification.objects.create(account=me, type="x", read=read)
        Notification.objects.create(account=other, type="x", read=False)
        r = client.get(self.URL, **_hdr(me))
        assert r.status_code == 200
        assert r.json() == {"unread": 2}

    def test_marking_read_brings_it_to_zero(self, client):
        me = AccountFactory()
        Notification.objects.create(account=me, type="x", read=False)
        client.post("/api/v1/me/notifications/read", **_hdr(me))
        assert client.get(self.URL, **_hdr(me)).json() == {"unread": 0}

    def test_needs_a_session(self, client):
        assert client.get(self.URL).status_code == 401


@pytest.mark.django_db
def test_the_dashboard_counts_active_shifts_like_the_upcoming_list(client):
    shelter = AccountFactory(account_type="shelter", email_verified_at=timezone.now())
    now = timezone.now()

    def shift(status, ends_in):
        ends = now + ends_in
        VolunteerShift.objects.create(shelter_account=shelter, type="walking", starts_at=ends - timedelta(hours=2),
                                      ends_at=ends, capacity=3, status=status)

    for _ in range(23):
        shift(ShiftStatus.OPEN, timedelta(days=3))                  # more than a page of 20
    shift(ShiftStatus.FULL, timedelta(days=1))
    shift(ShiftStatus.CLOSED, timedelta(days=1))                    # not active
    shift(ShiftStatus.OPEN, -timedelta(hours=1))                    # ended
    body = client.get("/api/v1/shelter/dashboard", **_hdr(shelter)).json()
    assert body["volunteer"]["active_shifts"] == 24
