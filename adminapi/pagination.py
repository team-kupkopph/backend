"""Opaque cursor pagination for the console's queues (US-C1).

Cursor, not offset: a reviewer works a queue while it changes underneath them, and
`?page=2` on a shifting list silently skips or repeats rows — in a verification backlog that
means a request nobody ever sees. The cursor is a position in a stable ordering, so
insertions cannot make a row disappear from the page after it.
"""
import base64
import json

from django.db.models import Q

PAGE_SIZE = 25


def encode_cursor(value) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def decode_cursor(cursor: str):
    if not cursor:
        return None
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except Exception:
        # A malformed cursor is a client bug, not a server one — start from the top rather
        # than 500. The alternative (an error) strands a reviewer on a broken bookmark.
        return None


def paginate(queryset, cursor_value, key_fields, page_size=PAGE_SIZE):
    """Slice `queryset` (already ordered ascending by `key_fields`) after `cursor_value`.

    Returns (rows, next_cursor). `key_fields` must together be a UNIQUE ordering — a
    non-unique one (submitted_at alone) makes ties ambiguous and drops rows at a boundary.

    ⚠️ ROW-VALUE COMPARISON, not a conjunction of per-field ones. The obvious
    `submitted_at > X AND verification_id > Y` is WRONG and was the first implementation
    here: it excludes every row whose `submitted_at` equals the cursor's, so the request
    sitting exactly on a page boundary is silently skipped. The test that walks all 30 rows
    caught it at 29 — on a backlog screen that is a verification nobody ever sees.

    The correct predicate is lexicographic:

        (a, b) > (x, y)  ==  a > x  OR  (a = x AND b > y)
    """
    if cursor_value is not None:
        predicate = Q()
        for i, field in enumerate(key_fields):
            clause = Q(**{f"{field}__gt": cursor_value[i]})
            for earlier, value in zip(key_fields[:i], cursor_value[:i]):
                clause &= Q(**{earlier: value})
            predicate |= clause
        queryset = queryset.filter(predicate)

    rows = list(queryset[: page_size + 1])
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor([str(getattr(last, f)) for f in key_fields])
    return rows, next_cursor
