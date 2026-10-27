"""T8 · "this URL is THIS account's own upload of THIS purpose" — one rule, used wherever a client
hands the server a stored-file URL that will later be rendered to other people.

Extracted from the donation-QR check (AT10) when a shelter's logo needed the same guarantee. The
object key the presign endpoint hands out is `{purpose}/{account_id}/{uuid}` (`verifications/
views.py::PresignView`), so the purpose and the owner are part of the key and another account's —
or another purpose's — upload fails here, rather than a stranger's file being rendered as ours.
"""

MAX_FILE_URL = 500


def own_upload_url_error(url, account, purpose, noun="image"):
    """An error string, or None when `url` is acceptable. Plain http is allowed only in DEBUG (the
    local dev receiver); with a public bucket configured the host must be that bucket."""
    from urllib.parse import urlsplit

    from django.conf import settings

    invalid = f"That {noun} link is not valid."
    if not isinstance(url, str) or len(url) > MAX_FILE_URL:
        return invalid
    try:
        parts = urlsplit(url)
        port = parts.port  # noqa: F841 — raises on a malformed port
    except ValueError:
        return invalid
    https_ok = parts.scheme == "https" or (settings.DEBUG and parts.scheme == "http")
    if not https_ok or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        return invalid
    bucket = getattr(settings, "MEDIA_S3_BUCKET_PUBLIC", "")
    if bucket and parts.hostname != bucket:
        return f"That {noun} was not uploaded through Kupkop."
    segments = [seg for seg in parts.path.split("/") if seg]
    if ".." in segments or "." in segments:
        return invalid
    # .../{purpose}/{account_id}/{object}
    for i in range(len(segments) - 2):
        if segments[i] == purpose and segments[i + 1] == str(account.pk) and len(segments) == i + 3:
            return None
    return f"That {noun} was not uploaded for this account."
