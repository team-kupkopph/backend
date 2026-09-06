
from common.storage import SIGNED_URL_TTL, signed_get_url


def test_dev_slice_returns_the_stored_reference_unchanged(settings):
    # No bucket configured (the dev slice: POST /media/presign returns example.invalid
    # placeholders). Signing degrades to the stored ref so review still renders.
    settings.MEDIA_S3_BUCKET_RESTRICTED = ""
    url = "https://example.invalid/dev-uploads/abc/gov_id"
    assert signed_get_url(url) == url


def test_ttl_is_short_lived():
    # The signed URL must not outlive the review page (US-R3).
    assert 0 < SIGNED_URL_TTL <= 900
