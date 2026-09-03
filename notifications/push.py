import logging
from dataclasses import dataclass

from django.conf import settings

logger = logging.getLogger("kupkop.push")


@dataclass(frozen=True)
class PushResult:
    ok: bool
    unregistered: bool = False


def _configured() -> bool:
    return bool(getattr(settings, "FCM_PROJECT_ID", "") and getattr(settings, "FCM_CREDENTIALS_PATH", ""))


def _fcm_transport(project_id, token, message):
    # Real FCM HTTP v1 POST goes here; guarded by _configured(). Tests patch this.
    # Deploy-time: build the OAuth2 request with the service-account creds and POST to
    # https://fcm.googleapis.com/v1/projects/{project_id}/messages:send .
    raise NotImplementedError("live FCM transport not configured")


def send_push(token, *, title, body, data) -> PushResult:
    if not _configured():
        return PushResult(ok=False)                       # dev no-op — nothing is sent
    try:
        resp = _fcm_transport(settings.FCM_PROJECT_ID, token,
                              {"notification": {"title": title, "body": body}, "data": data or {}})
    except Exception:
        logger.exception("push transport failed for a token")
        return PushResult(ok=False)
    if resp.get("status") == 200:
        return PushResult(ok=True)
    if resp.get("error") == "UNREGISTERED" or resp.get("status") == 404:
        return PushResult(ok=False, unregistered=True)
    return PushResult(ok=False)
