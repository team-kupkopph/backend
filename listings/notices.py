"""What each side of a public adoption is told (dev/adoption-build-review.md AD1–AD6, AQ1–AQ5).
The views decide WHEN; this module decides the words. It plays the same role as sagip/notices.py
for rescues."""
from notifications.service import notify
from shelter.models import ShelterProfile


def _name(listing):
    return listing.name or "this animal"


def _cap(text):
    return text[:1].upper() + text[1:]


def _poster_name(listing):
    profile = ShelterProfile.objects.filter(account_id=listing.posted_by_id).first()
    return profile.org_name if profile else listing.posted_by.display_name


def _data(inquiry, **extra):
    return {"listing_id": str(inquiry.listing_id), "inquiry_id": str(inquiry.pk), **extra}


def inquiry_accepted(inquiry):
    """AQ1 · the adopter hears they were accepted for screening, and that phones are now shared."""
    listing = inquiry.listing
    notify(inquiry.adopter_account, "inquiry_accepted",
           title=f"{_poster_name(listing)} wants to get to know you",
           body=(f"They accepted your inquiry about {_name(listing)}. "
                 f"You can now see each other's phone numbers."),
           data=_data(inquiry))
