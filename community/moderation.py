"""Review step 4 · the staff restore for a story a moderation takedown hid. The story counterpart of
listings/moderation.py and sagip/moderation.py: hide, never delete, and a mistaken takedown can be undone.

Only a moderation takedown ever sets a story `hidden` (`moderation.targets._hide_story`), so restoring one cannot
override an author's own choice."""
from django.db import transaction

from community.models import StoryPost, StoryStatus


class StoryNotHidden(Exception):
    """`restore_story` on a story no takedown is holding."""


@transaction.atomic
def restore_story(story, by, reason):
    """Put a hidden story back in the feed. It clears the hidden state and nothing else: the author is not told
    (a takedown does not tell them either, MO7), and the flags about it stay as the audit trail. Raises
    `StoryNotHidden` when there is nothing to restore."""
    locked = StoryPost.objects.select_for_update().filter(pk=story.pk).first()
    if locked is None or locked.status != StoryStatus.HIDDEN:
        raise StoryNotHidden()
    locked.status = StoryStatus.PUBLISHED
    locked.save(update_fields=["status"])
    story.status = StoryStatus.PUBLISHED
    return story
