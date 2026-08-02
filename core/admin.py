from django.contrib import admin

from .models import CheckoutSession, PaymentTransaction


@admin.register(CheckoutSession)
class CheckoutSessionAdmin(admin.ModelAdmin):
    list_display = ["session_id", "status", "amount", "currency", "created_at", "paid_at"]
    list_filter = ["status", "currency"]
    search_fields = ["session_id"]
    readonly_fields = ["session_id", "created_at", "updated_at", "paid_at"]


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = ["id", "session", "event_type", "created_at"]
    list_filter = ["event_type"]
    readonly_fields = ["session", "event_type", "provider_response", "created_at"]
