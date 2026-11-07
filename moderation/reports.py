"""Review step 15 (MO12) · filing a report, and what happens to the second one.

One live report per person per target: a double tap must not write two rows, and the queue should not fill with one person's
repeats. But a repeat is often NEW information (a second sentence, a different kind of problem), and the old collapse threw it
away while telling the person "Thanks, we will take a look". Now the repeat is added to the report that already exists:
its words are appended (never past the column's limit), a different category is tagged in the note, and a report a moderator had
only `reviewed` goes back in front of them when there is something new on it.

"Live" is open or reviewed. A decided report (actioned, dismissed) is history: a new report after one is a new concern.
The database holds the rule (`uq_flag_one_live_per_reporter_target`), so two taps in the same instant cannot write two rows."""
from django.db import IntegrityError, transaction

from moderation.models import DECIDABLE, FlagStatus, ModerationFlag

#: The longest a note can be (the API's own limit on `reason`).
NOTE_MAX = 2000

LIVE_CONSTRAINT = "uq_flag_one_live_per_reporter_target"

#: A category in words, for tagging a repeat report whose category differs. The same wording the app and the console use.
LABELS = {
    "spam_scam": "Spam or a scam",
    "animal_welfare": "An animal may be in danger",
    "harassment": "Harassment or abuse",
    "misleading": "Misleading or fake",
    "other": "Something else",
}


def merged_note(existing, existing_code, new_code, new_note):
    """The note after adding a repeat report: the new words on a new line, tagged with their category when it differs from the
    report's own. Unchanged when there is nothing new (the same words again, a double tap) or no room left."""
    part = (new_note or "").strip()
    if new_code != existing_code:
        part = f"[{LABELS.get(new_code, new_code)}] {part}".strip()
    if not part or part in existing:
        return existing
    return f"{existing}\n{part}"[:NOTE_MAX] if existing else part[:NOTE_MAX]


def add_to_flag(flag, reason_code, note):
    """Fold a repeat report into `flag`. Returns True if it changed anything. A `reviewed` flag with something new on it is
    open again: otherwise the new words would sit on a report a moderator believes they have already seen."""
    merged = merged_note(flag.reason, flag.reason_code, reason_code, note)
    if merged == flag.reason:
        return False
    flag.reason = merged
    fields = ["reason"]
    if flag.status == FlagStatus.REVIEWED:
        flag.status = FlagStatus.OPEN
        fields.append("status")
    flag.save(update_fields=fields)
    return True


def has_live(reporter, target_type, target_id):
    """Whether the reporter has a live report about this target. A plain read, so it is safe outside a transaction
    (`first_live` locks the row, and Django refuses a lock in autocommit)."""
    return ModerationFlag.objects.filter(reporter_account=reporter, target_type=target_type, target_id=target_id,
                                         status__in=DECIDABLE).exists()


def first_live(reporter, target_type, target_id):
    """The reporter's live report about this target, locked, or None."""
    return (ModerationFlag.objects.select_for_update()
            .filter(reporter_account=reporter, target_type=target_type, target_id=target_id, status__in=DECIDABLE)
            .first())


@transaction.atomic
def file_report(reporter, target_type, target_id, reason_code, note):
    """File a report, or add to the one the reporter already has. Returns `(flag, created)`."""
    existing = first_live(reporter, target_type, target_id)
    if existing is not None:
        add_to_flag(existing, reason_code, note)
        return existing, False
    from moderation.targets import snapshot_target
    snapshot = snapshot_target(target_type, target_id)      # step 17 · the evidence as it stood when it was reported
    try:
        with transaction.atomic():
            return ModerationFlag.objects.create(
                reporter_account=reporter, target_type=target_type, target_id=target_id,
                reason=note, reason_code=reason_code, target_snapshot=snapshot), True
    except IntegrityError:
        # The same person's other tap won the race. Its row is committed by now; fold into it.
        winner = first_live(reporter, target_type, target_id)
        if winner is None:
            raise                                  # not our constraint after all: do not hide it
        add_to_flag(winner, reason_code, note)
        return winner, False


def merge_live_duplicates(flag_model):
    """For the migration that adds the constraint, and for a test: where one person already has more than one live report about
    the same target, keep the oldest, fold the others' words into it, and dismiss the others. Returns how many were folded.

    Dismissed with nobody named: it is a tidy-up, not a moderator's decision. The kept report is open if ANY in the group
    was open (an unattended concern outranks one that had been looked at). System flags (no reporter) are never touched."""
    from django.utils import timezone

    folded = 0
    live = (flag_model.objects.filter(status__in=DECIDABLE, reporter_account__isnull=False)
            .order_by("reporter_account_id", "target_type", "target_id", "created_at", "flag_id"))
    groups = {}
    for flag in live:
        groups.setdefault((flag.reporter_account_id, flag.target_type, flag.target_id), []).append(flag)
    for rows in groups.values():
        if len(rows) < 2:
            continue
        keeper, rest = rows[0], rows[1:]
        for other in rest:
            keeper.reason = merged_note(keeper.reason, keeper.reason_code, other.reason_code, other.reason)
            if other.status == FlagStatus.OPEN:
                keeper.status = FlagStatus.OPEN
            other.status, other.reviewed_at, other.reviewed_by = FlagStatus.DISMISSED, timezone.now(), None
            other.save(update_fields=["status", "reviewed_at", "reviewed_by"])
            folded += 1
        keeper.save(update_fields=["reason", "status"])
    return folded
