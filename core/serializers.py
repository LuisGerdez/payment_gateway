from decimal import Decimal
import json

from rest_framework import serializers

from .models import CheckoutSession


class CreateCheckoutSessionSerializer(serializers.Serializer):
    """Input serializer for creating a checkout session."""

    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    currency = serializers.CharField(max_length=3, default="EUR")
    success_url = serializers.URLField()
    cancel_url = serializers.URLField(required=False, default="", allow_blank=True)
    metadata = serializers.DictField(required=False, default=dict, child=serializers.JSONField())
    expires_at = serializers.DateTimeField(required=False, default=None, allow_null=True)

    def validate_metadata(self, value):
        if len(json.dumps(value)) > 4096:
            raise serializers.ValidationError("Metadata is too large (max 4096 bytes).")
        return value


class CheckoutSessionSerializer(serializers.ModelSerializer):
    """Output serializer for a checkout session (create + retrieve)."""

    class Meta:
        model = CheckoutSession
        fields = [
            "session_id",
            "status",
            "amount",
            "currency",
            "payment_url",
            "success_url",
            "cancel_url",
            "metadata",
            "expires_at",
            "paid_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields
