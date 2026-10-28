"""Which shifts the public may see and request — written once (D3, review G16).

A shift is public when it is still bookable (open or full, starting in the future) AND its
shelter is active AND the shelter's organisation is verified. `Exists` rather than a join:
the browse queryset is annotated with an approved-signup Count, and a join to a shelter's
verification rows would multiply that count by the number of approved verifications.
"""
from django.db.models import Exists, OuterRef
from django.utils import timezone

from accounts.visibility import public_account_q
from verifications.models import VerificationRequest
from volunteer.models import ShiftStatus, VolunteerShift


def _verified_org():
    return Exists(VerificationRequest.objects.filter(
        account=OuterRef("shelter_account"), type="shelter_org", status="approved"))


def public_shifts():
    return (VolunteerShift.objects
            .filter(status__in=[ShiftStatus.OPEN, ShiftStatus.FULL],
                    starts_at__gt=timezone.now())
            .filter(public_account_q("shelter_account__"))
            .filter(_verified_org()))
