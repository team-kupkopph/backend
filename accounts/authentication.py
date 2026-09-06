from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken

from accounts.models import Account


class AccountJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        # `.get`, not `[...]`. US-K3 removed SIMPLE_JWT's USER_ID_CLAIM, so nothing guarantees
        # this claim is present — and since US-B1 the console mints staff tokens that
        # deliberately carry `staff_user_id`/`admin_account_id` instead. Indexing turned any
        # such token into an uncaught KeyError, i.e. a 500 with a traceback where a 401
        # belongs. A token this authenticator cannot read is invalid, not a server fault.
        account_id = validated_token.get("account_id")
        if account_id is None:
            raise InvalidToken("not_an_account_token")
        account = Account.objects.filter(account_id=account_id).first()
        if account is None:
            raise InvalidToken("account_not_found")
        revoked = account.sessions_revoked_at
        iat = validated_token.get("iat")
        if revoked is not None and iat is not None and int(iat) <= int(revoked.timestamp()):
            raise InvalidToken("session_revoked")
        return account
