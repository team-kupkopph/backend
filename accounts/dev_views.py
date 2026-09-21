"""Dev-only endpoint for seeding auth tokens without walking an OTP screen.

`POST /api/v1/auth/dev/seed_tokens` returns a fresh access/refresh pair for an
existing Account, looked up by email. It exists purely so automated and manual
test runs can skip typing a real OTP on a simulator/emulator that can't
receive one — see dev/test-plan-auth.md and dev/test-plan-guest.md for the
rows this unblocks.

This must be invisible in production: settings.DEBUG is checked FIRST, before
the method check or anything else, and the failure is a bare `Http404` with no
body — so any request against this path in prod (right method or wrong) 404s
exactly like an unmatched URL. A prod probe can't even learn the route exists.
"""
import json

from django.conf import settings
from django.http import Http404, HttpResponseNotAllowed, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from accounts.models import Account
from accounts.serializers import account_repr
from accounts.tokens import tokens_for


@csrf_exempt
def dev_seed_tokens(request):
    if not settings.DEBUG:
        raise Http404

    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        payload = None

    email = payload.get("email") if isinstance(payload, dict) else None
    if not email or not isinstance(email, str):
        return JsonResponse({"detail": "email required"}, status=400)

    account = Account.objects.filter(email__iexact=email).first()
    if account is None:
        return JsonResponse({"detail": "account not found"}, status=404)

    return JsonResponse({**tokens_for(account), "account": account_repr(account)})
