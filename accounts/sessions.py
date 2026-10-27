"""X1 · the session gate — the ONE place an Account session is minted.

Before this module every sign-in route called `tokens_for` itself, and only login remembered to
check `account.status`. Social sign-in and email verification did not, so a suspended account
could mint a fresh session with Google, and a token minted after `sessions_revoked_at` sails past
the revocation check. Status is now checked here, at minting, and again on every request
(`AccountJWTAuthentication`) and refresh (`AccountTokenRefreshSerializer`) — the lock is the
status, not just the revocation timestamp.

A guard test (`accounts/tests/test_session_gate.py`) fails if runtime code imports `tokens_for`
anywhere but here, so a new sign-in route cannot quietly skip the gate.
"""
from accounts.models import AccountStatus
from accounts.tokens import tokens_for


class SessionRefused(Exception):
    """The account exists but may not hold a session (suspended or deleted)."""


def can_hold_session(account):
    return account.status == AccountStatus.ACTIVE


def issue_session(account):
    """Access + refresh tokens for `account`, or `SessionRefused`."""
    if not can_hold_session(account):
        raise SessionRefused(account.status)
    return tokens_for(account)
