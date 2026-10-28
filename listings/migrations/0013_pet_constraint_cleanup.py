# PP15 · rows the constraints in 0014 would refuse, fixed first.

from django.db import migrations, models

OPEN = ("draft", "available", "pending")


def clean_rows_the_constraints_refuse(apps, schema_editor):
    """PP15 · make existing rows satisfy the new constraints before they are added, and say what
    changed (the community.0009 pattern):
    - a blank pet name becomes "Pet" (what `_pet_from_listing` already writes for a nameless one);
    - a weight of zero or less becomes unknown (NULL);
    - a pet with several primary photos keeps the earliest as primary;
    - a pet with several open rehoming listings stays linked to the oldest; the newer ones are
      unlinked from it (their status is not touched: a listing is never closed behind its poster)."""
    Pet = apps.get_model("listings", "Pet")
    PetPhoto = apps.get_model("listings", "PetPhoto")
    Listing = apps.get_model("listings", "AdoptionListing")
    names = Pet.objects.filter(name__regex=r"^\s*$").update(name="Pet")
    weights = Pet.objects.filter(weight_kg__lte=0).update(weight_kg=None)
    primaries = 0
    for pet_id in (PetPhoto.objects.filter(is_primary=True).values("pet_id")
                   .annotate(n=models.Count("pk")).filter(n__gt=1).values_list("pet_id", flat=True)):
        keep = PetPhoto.objects.filter(pet_id=pet_id, is_primary=True).order_by("uploaded_at").first()
        primaries += PetPhoto.objects.filter(pet_id=pet_id, is_primary=True).exclude(pk=keep.pk) \
            .update(is_primary=False)
    unlinked = 0
    for pet_id in (Listing.objects.filter(status__in=OPEN, rehome_pet__isnull=False).values("rehome_pet_id")
                   .annotate(n=models.Count("pk")).filter(n__gt=1).values_list("rehome_pet_id", flat=True)):
        keep = Listing.objects.filter(rehome_pet_id=pet_id, status__in=OPEN).order_by("created_at").first()
        unlinked += Listing.objects.filter(rehome_pet_id=pet_id, status__in=OPEN).exclude(pk=keep.pk) \
            .update(rehome_pet=None)
    print(f"\n  listings.0013: {names} blank name(s), {weights} bad weight(s), {primaries} extra "
          f"primary photo(s), {unlinked} extra open rehoming listing(s) unlinked")


class Migration(migrations.Migration):

    dependencies = [
        ('listings', '0012_pet_manage_rehomed_trace'),
    ]

    operations = [
        migrations.RunPython(clean_rows_the_constraints_refuse, migrations.RunPython.noop),
    ]
