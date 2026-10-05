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


REJECT_REASON_TEXT = {
    "not_a_fit": "they don't think it's the right match",
    "requirements_not_met": "the adoption requirements aren't met",
    "no_response": "they couldn't reach you",
    "other": "they decided not to go ahead",
}


def inquiry_rejected(inquiry):
    """AD6 · the adopter hears why. Another adopter being chosen (AQ3) is a happier sentence: the
    animal found a home."""
    listing = inquiry.listing
    if inquiry.end_reason == "another_adopter_chosen":
        title = f"{_cap(_name(listing))} found a home"
        body = f"{_cap(_name(listing))} was adopted by someone else. Thank you for offering them one."
    else:
        reason = REJECT_REASON_TEXT.get(inquiry.end_reason, REJECT_REASON_TEXT["other"])
        title = "About your inquiry"
        body = (f"{_poster_name(listing)} won't be going ahead with your inquiry about "
                f"{_name(listing)}: {reason}.")
    notify(inquiry.adopter_account, "inquiry_rejected", title=title, body=body, data=_data(inquiry))


def inquiry_withdrawn(inquiry, reopened):
    """AD6 · the poster hears an applicant stepped back. If the animal was reserved for them, it
    is back on the feed (AQ4)."""
    listing = inquiry.listing
    body = f"{inquiry.adopter_account.display_name} is no longer applying for {_name(listing)}."
    if reopened:
        body += f" {_cap(_name(listing))} is back on the Adopt feed."
    # poster_is_shelter drives Task 8's interim routing (Requests for a shelter, the listing for
    # an individual) until the Applicant screen exists.
    notify(listing.posted_by, "inquiry_withdrawn", title="An applicant withdrew", body=body,
           data=_data(inquiry, poster_is_shelter=listing.posted_by.account_type == "shelter"))


def adoption_badge_needed(inquiry):
    """AQ2 · the poster tried to reserve, and the adopter lacks the Verified Member badge."""
    listing = inquiry.listing
    notify(inquiry.adopter_account, "adoption_badge_needed",
           title=f"One step before you can adopt {_name(listing)}",
           body=(f"{_poster_name(listing)} is ready to reserve {_name(listing)} for you. "
                 f"Get your Verified Member badge to continue."),
           data=_data(inquiry))


def adoption_reserved(inquiry):
    listing = inquiry.listing
    notify(inquiry.adopter_account, "adoption_reserved",
           title=f"{_cap(_name(listing))} is reserved for you",
           body=f"{_poster_name(listing)} is holding {_name(listing)} for you while you arrange the adoption.",
           data=_data(inquiry))


def reservation_released(inquiry):
    listing = inquiry.listing
    notify(inquiry.adopter_account, "reservation_released",
           title=f"{_cap(_name(listing))} is no longer reserved",
           body=f"{_poster_name(listing)} released the reservation. Your inquiry is still open.",
           data=_data(inquiry))
