"""Shared builders for the volunteer tests. P1 made the public shift feed depend on the
posting shelter being verified and active, so tests that browse or create need a shelter
that a real volunteer could actually see."""
from accounts.factories import AccountFactory
from accounts.tokens import tokens_for
from verifications.models import VerificationRequest


def hdr(account):
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens_for(account)['access']}"}


def verified_shelter(**kw):
    account = AccountFactory(account_type="shelter", **kw)
    VerificationRequest.objects.create(account=account, type="shelter_org", status="approved")
    return account
