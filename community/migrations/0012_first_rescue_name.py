"""IB16 / IQ7 · rename the rescue badge "Rescuer" to "First Rescue".

"Rescuer" is also a capability and a verified standing, so the badge read as a status the holder might not have. Only the
name changes; the code, rule and held badges stay. A badge's name is copied into the `badge_earned` notice body when it is
awarded, so notices already sent keep the old word: they are a record of what the person was told.
"""
from django.db import migrations

CODE = "first_rescue"


def rename(old, new):
    def run(apps, schema_editor):
        apps.get_model("community", "Badge").objects.filter(pk=CODE, name=old).update(name=new)
    return run


class Migration(migrations.Migration):
    dependencies = [("community", "0011_donor_badge")]
    operations = [migrations.RunPython(rename("Rescuer", "First Rescue"), rename("First Rescue", "Rescuer"))]
