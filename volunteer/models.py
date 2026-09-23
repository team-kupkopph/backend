import uuid

from django.db import models

from accounts.models import Account


class VolunteerType(models.TextChoices):
    WALKING = "walking"
    FEEDING = "feeding"
    VISITOR = "visitor"
    EVENT = "event"
    FACILITY = "facility"
    TRANSPORT = "transport"


class ShiftStatus(models.TextChoices):
    OPEN = "open"
    CLOSED = "closed"
    FULL = "full"


class SignupStatus(models.TextChoices):
    REQUESTED = "requested"
    APPROVED = "approved"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"


class VolunteerShift(models.Model):
    """A shelter's posted Kawang-Gawa activity (kupkop_mvp_schema.sql `volunteer_shift`).

    `closed` is TERMINAL — an activity is never reopened. `full` is reached when approvals
    equal `capacity`, and is computed under a row lock at approval time (see
    volunteer/views.py::ShiftApproveView), never by an unlocked count.
    """

    shift_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shelter_account = models.ForeignKey(Account, on_delete=models.CASCADE,
                                        related_name="volunteer_shifts")
    type = models.CharField(max_length=12, choices=VolunteerType.choices,
                            default=VolunteerType.WALKING)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    capacity = models.IntegerField(default=1)
    status = models.CharField(max_length=10, choices=ShiftStatus.choices,
                              default=ShiftStatus.OPEN)

    # P2 · what / where (review G1). A shift used to be only a type, a time and an org name —
    # a volunteer could not tell where to go or what they would do. Location is copied from the
    # shelter's primary address at posting time (editable), so moving the shelter later does
    # not silently move shifts already booked.
    title = models.CharField(max_length=80, blank=True)
    description = models.TextField(blank=True)
    meeting_point = models.CharField(max_length=160, blank=True)
    address_line1 = models.CharField(max_length=200, blank=True)
    barangay = models.CharField(max_length=80, blank=True)
    city = models.CharField(max_length=80, blank=True, db_index=True)
    province = models.CharField(max_length=80, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "volunteer_shift"
        indexes = [models.Index(fields=["shelter_account", "starts_at"],
                                name="idx_volunteer_shift_shelter"),
                  models.Index(fields=["status"], name="idx_volunteer_shift_status")]
        constraints = [
            models.CheckConstraint(condition=models.Q(ends_at__gt=models.F("starts_at")),
                                   name="volunteer_shift_ends_after_starts"),
            models.CheckConstraint(condition=models.Q(capacity__gte=1),
                                   name="volunteer_shift_capacity_min1"),
        ]


class VolunteerSignup(models.Model):
    """One volunteer's request against a shift (`volunteer_signup`).

    Two SEPARATE consents, deliberately not collapsed into one flag:
      - `waiver_accepted` — the liability waiver. A versioned legal document (D-S5-1), so it
        carries `waiver_accepted_at` + `waiver_version`, matching account.terms_consent_*.
      - `contact_share_consent` — a per-shift §12.5 exception to `masked_contact`, letting the
        shelter see phone/email/address for THIS shift only. Timestamped, not versioned:
        it is a data-sharing opt-in, not a document.

    `cancelled_at` is stored explicitly rather than read off `updated_at`, because any later
    write overwrites `updated_at` and the 12h free-vs-late audit must survive that — the same
    reason `adoption_inquiry.decided_at` exists.
    """

    signup_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shift = models.ForeignKey(VolunteerShift, on_delete=models.CASCADE, related_name="signups")
    volunteer_account = models.ForeignKey(Account, on_delete=models.PROTECT,
                                          related_name="volunteer_signups")
    assigned_listing = models.ForeignKey("listings.AdoptionListing", on_delete=models.SET_NULL,
                                         null=True, blank=True, related_name="+")
    status = models.CharField(max_length=10, choices=SignupStatus.choices,
                              default=SignupStatus.REQUESTED)
    waiver_accepted = models.BooleanField(default=False)
    waiver_accepted_at = models.DateTimeField(null=True, blank=True)
    waiver_version = models.CharField(max_length=20, blank=True)
    contact_share_consent = models.BooleanField(default=False)
    contact_share_consent_at = models.DateTimeField(null=True, blank=True)
    check_in_at = models.DateTimeField(null=True, blank=True)
    check_out_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "volunteer_signup"
        indexes = [models.Index(fields=["volunteer_account"], name="idx_volunteer_signup_walker"),
                  models.Index(fields=["status"], name="idx_volunteer_signup_status"),
                  models.Index(fields=["assigned_listing"], name="idx_volunteer_signup_listing")]
        constraints = [
            models.UniqueConstraint(fields=["shift", "volunteer_account"],
                                    name="idx_volunteer_signup_pair"),
        ]
