"""Review step 15 (MO12) · the constraint half: one live (open or reviewed) report per person per target. See 0006 for the data fix
that must run first, and why it is a separate migration."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('moderation', '0006_fold_duplicate_live_reports'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='moderationflag',
            constraint=models.UniqueConstraint(
                condition=models.Q(('status__in', ['open', 'reviewed'])),
                fields=('reporter_account', 'target_type', 'target_id'),
                name='uq_flag_one_live_per_reporter_target'),
        ),
    ]
