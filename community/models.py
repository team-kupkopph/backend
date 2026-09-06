import uuid

from django.db import models

from accounts.models import Account


class NeedCategory(models.TextChoices):
    FOOD = "food"
    MEDICINE = "medicine"
    SUPPLIES = "supplies"
    FUNDS = "funds"
    OTHER = "other"


class NeedStatus(models.TextChoices):
    OPEN = "open"
    FULFILLED = "fulfilled"
    CLOSED = "closed"


class PledgeStatus(models.TextChoices):
    PLEDGED = "pledged"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class ShelterNeed(models.Model):
    """A shelter's Abot-tulong wishlist item (kupkop_mvp_schema.sql `shelter_need`).

    `quantity_received` is a running total the shelter grows via received-confirm — never a
    pledge, which is only a promise (D-S6-7). The `open -> fulfilled` flip and all
    `quantity_received` arithmetic live in ONE writer (`community/needs.py::apply_received`),
    under a row lock, so two staff confirming at once can't double-count. `fulfilled`/`closed`
    take no new pledges.

    No `updated_at`: the DDL doesn't carry one for this table, and status/received changes are
    already auditable through the pledge rows that drove them.
    """

    need_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shelter_account = models.ForeignKey(Account, on_delete=models.CASCADE,
                                        related_name="shelter_needs")
    title = models.CharField(max_length=120)
    category = models.CharField(max_length=10, choices=NeedCategory.choices)
    description = models.TextField(blank=True)
    quantity_needed = models.IntegerField(default=1)
    quantity_received = models.IntegerField(default=0)
    status = models.CharField(max_length=10, choices=NeedStatus.choices,
                              default=NeedStatus.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "shelter_need"
        indexes = [
            models.Index(fields=["shelter_account", "status"], name="idx_shelter_need_shelter"),
            models.Index(fields=["category"], name="idx_shelter_need_category"),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity_needed__gte=1),
                                   name="shelter_need_needed_min1"),
            models.CheckConstraint(condition=models.Q(quantity_received__gte=0),
                                   name="shelter_need_received_min0"),
        ]


class NeedPledge(models.Model):
    """One giver's promise against a need (`need_pledge`).

    A pledge is a promise, not inventory: it never touches `shelter_need.quantity_received`
    (only the shelter's received-confirm does, D-S6-7). A `pledged` pledge can be cancelled by
    its pledger; a `delivered` one is a recorded fact and immutable (the attendance posture).
    """

    pledge_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    need = models.ForeignKey(ShelterNeed, on_delete=models.CASCADE, related_name="pledges")
    pledger_account = models.ForeignKey(Account, on_delete=models.PROTECT,
                                        related_name="need_pledges")
    quantity = models.IntegerField(default=1)
    status = models.CharField(max_length=10, choices=PledgeStatus.choices,
                              default=PledgeStatus.PLEDGED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "need_pledge"
        indexes = [
            models.Index(fields=["need"], name="idx_need_pledge_need"),
            models.Index(fields=["pledger_account"], name="idx_need_pledge_pledger"),
        ]
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gte=1),
                                   name="need_pledge_quantity_min1"),
        ]


class Badge(models.Model):
    """The badge catalog (kupkop_mvp_schema.sql `badge`). Seeded, not user-created.

    `badge_code` is the natural primary key. Criteria are shift-agnostic (D-S6-2): any
    completed Kawang-Gawa shift counts, not walks only.
    """

    badge_code = models.CharField(max_length=40, primary_key=True)
    name = models.TextField()
    description = models.TextField(blank=True)
    icon = models.TextField(blank=True)
    criteria = models.TextField(blank=True)

    class Meta:
        db_table = "badge"


class AccountBadge(models.Model):
    """One earned badge (`account_badge`). Awarding is an idempotent insert — the composite
    identity absorbs replays, so `award_badges_for` can run at an event AND in the nightly
    catch-up sweep (D-S6-3) without double-awarding.

    The DDL's PRIMARY KEY (account_id, badge_code) is enforced here as a UNIQUE constraint over
    Django's implicit surrogate key: Django 5.1 has no composite primary key (added in 5.2).
    Revisit as a real composite PK on the 5.2 upgrade; the uniqueness invariant is identical.
    """

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="badges")
    badge = models.ForeignKey(Badge, on_delete=models.PROTECT, related_name="+",
                              db_column="badge_code")
    earned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "account_badge"
        constraints = [
            models.UniqueConstraint(fields=["account", "badge"], name="uq_account_badge"),
        ]


class StoryType(models.TextChoices):
    ADOPTION = "adoption"
    RESCUE = "rescue"
    GENERAL = "general"


class StoryStatus(models.TextChoices):
    PUBLISHED = "published"
    HIDDEN = "hidden"


class StoryPost(models.Model):
    """A success story (kupkop_mvp_schema.sql `story_post`). User-generated content, so it is
    moderatable from day one (D-S6-4): a flag rides the existing moderation_flag pipeline with
    flag_target='story', and `status='hidden'` is the lever — the row and its photos survive,
    the feed just excludes it, and the author still sees a "hidden by moderation" state (never a
    silent vanish). A story carries no location of its own (D-S6-4): the feed shows the author's
    city; linked case/listing context goes through the already-gated detail screens.
    """

    story_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    author_account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="stories")
    adoption_listing = models.ForeignKey("listings.AdoptionListing", on_delete=models.SET_NULL,
                                         null=True, blank=True, related_name="+")
    rescue_case = models.ForeignKey("sagip.RescueCase", on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")
    story_type = models.CharField(max_length=10, choices=StoryType.choices)
    caption = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=StoryStatus.choices,
                              default=StoryStatus.PUBLISHED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "story_post"
        indexes = [
            models.Index(fields=["author_account"], name="idx_story_post_author"),
            models.Index(fields=["status", "-created_at"], name="idx_story_post_status"),
        ]


class StoryPhoto(models.Model):
    photo_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    story = models.ForeignKey(StoryPost, on_delete=models.CASCADE, related_name="photos")
    url = models.TextField()
    is_primary = models.BooleanField(default=False)

    class Meta:
        db_table = "story_photo"


class StoryReaction(models.Model):
    """One like per user per story (`story_reaction`). The DDL's composite PK
    (story_id, account_id) is enforced as a UNIQUE constraint over Django's surrogate (5.1 has
    no composite PK), which is what makes react/un-react idempotent."""

    story = models.ForeignKey(StoryPost, on_delete=models.CASCADE, related_name="reactions")
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="story_reactions")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "story_reaction"
        constraints = [
            models.UniqueConstraint(fields=["story", "account"], name="uq_story_reaction"),
        ]
        indexes = [models.Index(fields=["account"], name="idx_story_reaction_account")]
