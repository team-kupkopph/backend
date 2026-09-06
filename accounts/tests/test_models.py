import pytest
from django.db import IntegrityError

from accounts.models import Account


@pytest.mark.django_db
def test_create_account_is_email_identified_and_unverified_by_default():
    acc = Account.objects.create_account(
        account_type="personal", email="Ana@Example.com",
        password="s3cretpass", display_name="Ana",
    )
    assert acc.account_id is not None
    assert acc.email == "Ana@Example.com"           # citext preserves case, compares insensitively
    assert acc.email_verified_at is None
    assert acc.phone is None
    assert acc.check_password("s3cretpass")
    assert acc.settings.marketing_emails is False    # 1:1 settings created with schema defaults
    assert acc.settings.approximate_location is True


@pytest.mark.django_db
def test_email_is_case_insensitively_unique():
    Account.objects.create_account(account_type="personal", email="a@b.com",
                                   password="s3cretpass", display_name="A")
    with pytest.raises(IntegrityError):
        Account.objects.create_account(account_type="personal", email="A@B.COM",
                                       password="s3cretpass", display_name="A2")
