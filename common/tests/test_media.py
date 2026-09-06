"""US-D2 — media policy (per-purpose bucket routing, content-type/size limits) and the
EXIF-strip transform.
"""
import io

import pytest
from PIL import Image
from PIL.ExifTags import Base as ExifTag

from common.media import (
    DOC_CONTENT_TYPES,
    IMAGE_CONTENT_TYPES,
    MAX_DOC_BYTES,
    MAX_IMAGE_BYTES,
    PUBLIC,
    RESTRICTED,
    policy_for,
    strip_exif,
)


def _jpeg_with_gps_exif():
    """A real JPEG with a populated GPS IFD — Marikina's coordinates, same shape a
    phone-camera photo would carry if EXIF weren't stripped (gap 11)."""
    image = Image.new("RGB", (4, 4), color=(200, 100, 50))
    exif = image.getexif()
    gps_ifd = {
        1: "N", 2: (14.0, 39.0, 0.0),   # GPSLatitudeRef, GPSLatitude
        3: "E", 4: (121.0, 6.0, 0.0),   # GPSLongitudeRef, GPSLongitude
    }
    exif[ExifTag.GPSInfo.value] = gps_ifd
    out = io.BytesIO()
    image.save(out, format="JPEG", exif=exif)
    return out.getvalue()


def test_policy_for_known_purposes():
    visibility, types, max_bytes = policy_for("listing_photo")
    assert visibility == PUBLIC
    assert types == IMAGE_CONTENT_TYPES
    assert max_bytes == MAX_IMAGE_BYTES


def test_verification_doc_is_the_one_restricted_purpose_and_allows_pdf():
    visibility, types, max_bytes = policy_for("verification_doc")
    assert visibility == RESTRICTED
    assert types == DOC_CONTENT_TYPES
    assert "application/pdf" in types
    assert max_bytes == MAX_DOC_BYTES


def test_policy_for_unknown_purpose_is_none():
    assert policy_for("carrier_pigeon") is None


def test_every_public_purpose_disallows_pdf():
    for purpose in ["listing_photo", "stray_photo", "rescue_outcome_photo", "donation_qr"]:
        _, types, _ = policy_for(purpose)
        assert "application/pdf" not in types


def test_strip_exif_removes_the_gps_ifd():
    original = _jpeg_with_gps_exif()
    original_exif = Image.open(io.BytesIO(original)).getexif()
    assert ExifTag.GPSInfo.value in original_exif  # sanity: the fixture really has GPS

    stripped = strip_exif(original, "image/jpeg")

    stripped_exif = Image.open(io.BytesIO(stripped)).getexif()
    assert len(stripped_exif) == 0


def test_strip_exif_preserves_the_visible_pixels():
    original = _jpeg_with_gps_exif()
    stripped = strip_exif(original, "image/jpeg")
    before = Image.open(io.BytesIO(original)).convert("RGB")
    after = Image.open(io.BytesIO(stripped)).convert("RGB")
    assert before.size == after.size
    assert before.tobytes() == after.tobytes()


def test_strip_exif_passes_a_pdf_through_unchanged():
    pdf_bytes = b"%PDF-1.4 not a real pdf but has no EXIF concept"
    assert strip_exif(pdf_bytes, "application/pdf") == pdf_bytes


@pytest.mark.parametrize("content_type,fmt", [("image/png", "PNG"), ("image/webp", "WEBP")])
def test_strip_exif_round_trips_other_image_formats(content_type, fmt):
    image = Image.new("RGB", (3, 3), color=(10, 20, 30))
    out = io.BytesIO()
    image.save(out, format=fmt)
    stripped = strip_exif(out.getvalue(), content_type)
    assert Image.open(io.BytesIO(stripped)).format == fmt


def test_every_purpose_the_mobile_client_asks_for_is_registered():
    """US-E5 · regression: `story_photo` was missing from PURPOSES.

    `StoryComposeScreen` presigns with `purpose: "story_photo"`, the view answered
    `422 purpose_unknown`, and a story requires at least one photo — so posting a story,
    shipped in Sprint 6, could not succeed at all. Nothing caught it because the Sprint 6
    device walk exercised the story FEED and reactions, never the composer's upload.

    This list is the mobile client's actual callers (`grep 'media/presign' mobile_app/src`).
    Adding a presign call on the client without adding its purpose here is the same bug
    again, so the list is asserted rather than assumed.
    """
    from common.media import PURPOSES

    called_by_the_app = {
        "stray_photo",           # ReportStrayScreen
        "listing_photo",         # ListingFormScreen
        "rescue_outcome_photo",  # RescueUpdateScreen
        "donation_qr",           # DonationQrScreen
        "verification_doc",      # MemberVerify / ShelterVerify / ShelterVerifyNgo / VerifyResubmit
        "story_photo",           # StoryComposeScreen
    }
    missing = called_by_the_app - set(PURPOSES)
    assert not missing, f"purposes the app requests but the server rejects: {sorted(missing)}"


@pytest.mark.django_db
def test_a_story_photo_can_actually_be_presigned(client):
    from accounts.factories import AccountFactory
    from accounts.tokens import tokens_for

    account = AccountFactory()
    res = client.post("/api/v1/media/presign",
                      {"purpose": "story_photo", "content_type": "image/jpeg"},
                      content_type="application/json",
                      HTTP_AUTHORIZATION=f"Bearer {tokens_for(account)['access']}")

    assert res.status_code == 200, res.json()
    assert "upload_url" in res.json() and "file_url" in res.json()
