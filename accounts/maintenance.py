"""T7 · the two data cleanups `accounts/0010` runs, as plain functions taking the models so the
migration can hand in its historical ones and the tests the real ones (the same shape as
`community.needs.settle_stranded_pledges`)."""


def backfill_account_settings(account_model, settings_model):
    """X12 · give every account that lacks one its settings row, with the defaults.

    29 of 44 dev accounts were seeded by scripts that bypassed `create_account`, which is the only
    place the row is made. Idempotent; returns how many rows it created."""
    missing = account_model.objects.filter(settings__isnull=True)
    return len(settings_model.objects.bulk_create(
        [settings_model(account=account) for account in missing]))


def demote_duplicate_primary_addresses(address_model):
    """X15 · where an account has several primary addresses, keep ONE and demote the rest.

    Demoted, not deleted: an address is the person's own data and nothing here is wrong enough to
    erase it. `address` has no timestamp, so "newest" is not knowable; the lowest id survives, which
    is arbitrary but stable — a second run changes nothing. Returns how many rows it demoted."""
    from django.db.models import Count
    demoted = 0
    duplicated = (address_model.objects.filter(is_primary=True).values("account_id")
                  .annotate(n=Count("pk")).filter(n__gt=1).values_list("account_id", flat=True))
    for account_id in list(duplicated):
        rows = list(address_model.objects.filter(account_id=account_id, is_primary=True)
                    .order_by("pk").values_list("pk", flat=True))
        demoted += address_model.objects.filter(pk__in=rows[1:]).update(is_primary=False)
    return demoted
