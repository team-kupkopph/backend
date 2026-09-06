import uuid

from django.db import models

from accounts.models import Account


class DeviceToken(models.Model):
    """US-P1 · a device's FCM token. Matches §7's device_token (fcm_token UNIQUE, the
    device_platform enum modelled as choices). Re-registering a token re-homes it to the
    latest account — a shared phone must not keep pushing to its previous owner."""
    token_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="device_tokens")
    fcm_token = models.TextField(unique=True)
    platform = models.CharField(max_length=10, choices=[("ios", "ios"), ("android", "android")])
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "device_token"
