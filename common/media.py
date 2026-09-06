"""US-D2 · media policy — per-purpose bucket routing, content-type/size limits, and the
EXIF-strip transform every image purpose goes through before it's readable.
"""
import io

from PIL import Image

PUBLIC = "public"
RESTRICTED = "restricted"

IMAGE_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
DOC_CONTENT_TYPES = IMAGE_CONTENT_TYPES | {"application/pdf"}

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_DOC_BYTES = 20 * 1024 * 1024    # 20 MB

# purpose -> (visibility, allowed content types, size cap). Every real `POST
# /media/presign` purpose in the mobile client (grep `api.post("/media/presign"`) is
# here; `verification_doc` is the one RESTRICTED purpose and the only one allowed a PDF.
PURPOSES = {
    "listing_photo": (PUBLIC, IMAGE_CONTENT_TYPES, MAX_IMAGE_BYTES),
    "stray_photo": (PUBLIC, IMAGE_CONTENT_TYPES, MAX_IMAGE_BYTES),
    "rescue_outcome_photo": (PUBLIC, IMAGE_CONTENT_TYPES, MAX_IMAGE_BYTES),
    "donation_qr": (PUBLIC, IMAGE_CONTENT_TYPES, MAX_IMAGE_BYTES),
    # US-E5 · added Sprint 7. StoryComposeScreen has presigned with this purpose since
    # Sprint 6 and got 422 purpose_unknown every time — and a story requires at least one
    # photo, so posting a story could not succeed at all. Public, like every other photo
    # that appears in a feed.
    "story_photo": (PUBLIC, IMAGE_CONTENT_TYPES, MAX_IMAGE_BYTES),
    "verification_doc": (RESTRICTED, DOC_CONTENT_TYPES, MAX_DOC_BYTES),
}

_FORMAT_FOR_CONTENT_TYPE = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


def policy_for(purpose):
    """`(visibility, allowed_content_types, max_bytes)` for `purpose`, or `None` if the
    purpose isn't registered — the view turns that into `422 purpose_unknown`."""
    return PURPOSES.get(purpose)


def strip_exif(image_bytes, content_type):
    """Return `image_bytes` with all EXIF (incl. GPS) removed.

    ⚠️ A privacy control, not an optimization (gap 11): a report or rescue-space photo
    otherwise carries the reporter's precise coordinates around the coarsened-location
    rule (US-SEC1/§12.5) the API otherwise enforces — the photo would leak what the JSON
    response deliberately withholds. Runs on every image purpose; a PDF (the one
    `verification_doc` content type with no EXIF concept) passes through unchanged.

    Re-encodes via Pillow (dropping the source `info`/exif dict entirely) rather than
    surgically deleting individual EXIF tags — simpler, and correct here since nothing
    downstream depends on any other embedded metadata (ICC profile, XMP, …) surviving.

    This is the transform a post-upload worker/Lambda calls once a real bucket exists
    (US-D2's "buildable slice without live AWS credentials") — wiring the S3 event
    trigger itself is the deploy-time task, not this function.
    """
    if content_type == "application/pdf":
        return image_bytes
    image = Image.open(io.BytesIO(image_bytes))
    image.load()
    # Rebuild from raw pixel bytes rather than Image.copy() — copy() can carry the
    # source .info dict (where Pillow stashes exif) along with it; a fresh image built
    # from nothing but the pixels has no metadata to carry.
    clean = Image.frombytes(image.mode, image.size, image.tobytes())
    out = io.BytesIO()
    clean.save(out, format=_FORMAT_FOR_CONTENT_TYPE.get(content_type, image.format or "JPEG"))
    return out.getvalue()
