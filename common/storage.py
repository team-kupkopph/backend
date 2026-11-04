"""Media signing/upload seam — US-R3 (signed GET) + US-D2 (real presigned upload).

**Two buckets, by visibility** (US-D2): `MEDIA_S3_BUCKET_PUBLIC` (pet/listing/report
photos, avatars — CDN-cacheable, world-readable) and `MEDIA_S3_BUCKET_RESTRICTED`
(verification docs — gov ID, proof of billing — no public read, ever; reached only via
`signed_get_url`, TTL ≤ 5 min, minted fresh per page render). Every render/upload path
goes through this module, so the day real buckets are configured only this file changes —
nothing elsewhere ever builds or emits a raw S3 object URL itself.

**Still a dev stub with no bucket configured** — the same pattern the OTP sender and
social-auth seam use: `POST /media/presign` returns `https://example.invalid/...`
placeholders, `signed_get_url` returns the stored reference unchanged, `delete_object` is
a no-op. Wiring an actual bucket (env vars, IAM, lifecycle rules) stays a deploy-time task
for whoever has AWS access — this module's job is that the *code path* is real and tested
against a mocked S3 (`moto`), not that a bucket is live.
"""
import logging
from urllib.parse import urlsplit

from django.conf import settings

from common.media import PUBLIC, RESTRICTED  # noqa: F401 — re-exported for callers

# Signed URLs are minted fresh on each page render and must not outlive it.
SIGNED_URL_TTL = 300  # seconds (5 minutes)


def _bucket_setting(visibility):
    return "MEDIA_S3_BUCKET_RESTRICTED" if visibility == RESTRICTED else "MEDIA_S3_BUCKET_PUBLIC"


def _bucket_name(visibility):
    return getattr(settings, _bucket_setting(visibility), "")


def signed_get_url(file_url, visibility=RESTRICTED, expires_in=SIGNED_URL_TTL):
    """A short-lived signed GET URL for a stored object reference.

    Dev (no bucket configured for `visibility`): returns `file_url` unchanged.
    Production signs the S3 object with boto3 (imported lazily so the dev slice needs no
    AWS dependency). Defaults to `visibility=restricted` — every current caller
    (`verifications/admin.py`'s document preview) is signing a verification document.
    """
    bucket = _bucket_name(visibility)
    if not bucket:
        return file_url
    import boto3  # prod-only; not a dev dependency

    key = file_url.split(f"{bucket}/", 1)[-1]
    return boto3.client("s3").generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expires_in)


def delete_object(file_url, visibility=RESTRICTED):
    """Delete the stored object backing `file_url` (US-SEC4 retention purge).

    Same dev-stub seam as `signed_get_url`: no bucket configured means no real object
    ever existed behind the placeholder, so this is a no-op today. Defaults to
    `visibility=restricted` — every current caller (`verifications/purge.py`) purges
    identity documents.
    """
    bucket = _bucket_name(visibility)
    if not bucket:
        return
    import boto3  # prod-only; not a dev dependency

    boto3.client("s3").delete_object(Bucket=bucket, Key=object_key(file_url, visibility))


def delete_object_or_log(file_url, visibility, *, why, deleter=None):
    """Delete a superseded object, and on a storage error log its KEY and carry on (F3, shelter home test plan).

    For deletes that run after the change they clean up has committed (a replaced logo, a withdrawn QR's image):
    raising there turned a saved change into a 500. The log names the key, never the URL (it can carry a host and a
    signature) and never an account id, so the orphan can be found and removed by hand. `deleter` is the caller's
    own `delete_object` reference, so tests that patch it where it is imported keep working."""
    try:
        (deleter or delete_object)(file_url, visibility)
    except Exception as exc:  # noqa: BLE001 — boto3 is prod-only, so its error types can't be named
        logging.getLogger("kupkop.storage").warning(
            "could not delete %s object %s (%s); left for cleanup", why, object_key(file_url, visibility),
            type(exc).__name__)


def object_key(file_url, visibility=RESTRICTED):
    """The bucket key behind `file_url` — what `delete_object` deletes, and the only part of a
    stored reference that is safe to log (the URL can carry a host and a signature).

    Dev (no bucket configured): the URL's path, which is the same key in the placeholder scheme.
    """
    bucket = _bucket_name(visibility)
    if bucket and f"{bucket}/" in file_url:
        return file_url.split(f"{bucket}/", 1)[-1].split("?", 1)[0]
    return urlsplit(file_url).path.lstrip("/")


def create_presigned_upload(visibility, key, content_type, max_bytes, expires_in=SIGNED_URL_TTL):
    """A presigned POST policy for a client to upload `key` directly to S3 (US-D2) —
    `key` is always server-chosen (see `verifications.views.PresignView`), never a
    client-supplied path. Enforces `content_type` and a `[0, max_bytes]` size range as
    POST policy *conditions*, so S3 itself refuses an upload that doesn't match what the
    caller declared — the application-level checks in the view are the first gate (a
    clean 422 before any AWS call), this is the second, harder-to-bypass one.

    Dev (no bucket configured for `visibility`): the same `example.invalid` placeholder
    every other dev-stub path returns — `upload_url`/`fields` a client can still "submit"
    against nothing, `file_url` deterministic from `key`.
    """
    bucket = _bucket_name(visibility)
    if not bucket:
        return {"upload_url": "https://example.invalid/dev-upload", "fields": {},
                "file_url": f"https://example.invalid/{key}"}
    import boto3  # prod-only; not a dev dependency

    presigned = boto3.client("s3").generate_presigned_post(
        Bucket=bucket, Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[{"Content-Type": content_type}, ["content-length-range", 0, max_bytes]],
        ExpiresIn=expires_in)
    return {"upload_url": presigned["url"], "fields": presigned["fields"],
           "file_url": f"https://{bucket}/{key}"}
