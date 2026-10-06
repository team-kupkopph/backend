"""AD22 · which inquiries are placements. Before migration 0007 a placement was recognised by its
ladder (every stage SKIPPED). The `kind` column says it outright. This backfill is the old rule,
run once over the rows that predate the column."""
from django.db.models import Count, F, Q


def backfill_inquiry_kinds(inquiry_model, stage_model):
    """Mark every inquiry whose stages are all SKIPPED as a placement. Returns how many rows
    changed. Takes the models as arguments so the migration can pass its historical ones."""
    all_skipped = (stage_model.objects.values("inquiry_id")
                   .annotate(total=Count("pk"), skipped=Count("pk", filter=Q(state="skipped")))
                   .filter(total__gt=0, total=F("skipped"))
                   .values("inquiry_id"))
    return (inquiry_model.objects.filter(pk__in=all_skipped)
            .exclude(kind="placement").update(kind="placement"))
