from django.contrib import admin

from .models import CheckoutSession, PaymentTransaction


class PaymentTransactionInline(admin.TabularInline):
    model = PaymentTransaction
    extra = 0
    can_delete = False
    fields = (
        "created_at",
        "event_type",
    )
    readonly_fields = fields
    show_change_link = True
    ordering = ("-created_at",)


@admin.register(CheckoutSession)
class CheckoutSessionAdmin(admin.ModelAdmin):
    list_display = (
        "session_id",
        "status",
        "amount",
        "currency",
        "created_at",
        "paid_at",
        "expires_at",
        "transaction_count",
    )

    list_filter = (
        "status",
        "currency",
        "created_at",
        "paid_at",
    )

    search_fields = (
        "session_id",
    )

    readonly_fields = (
        "session_id",
        "created_at",
        "updated_at",
        "paid_at",
        "payment_url",
    )

    ordering = (
        "status",
        "-created_at",
    )

    inlines = [PaymentTransactionInline]

    fieldsets = (
        ("Session", {
            "fields": (
                "session_id",
                "status",
                ("amount", "currency"),
            )
        }),
        ("URLs", {
            "fields": (
                "payment_url",
                "success_url",
                "cancel_url",
            )
        }),
        ("Metadata", {
            "fields": ("metadata",),
        }),
        ("Dates", {
            "fields": (
                "created_at",
                "updated_at",
                "expires_at",
                "paid_at",
            )
        }),
    )

    @admin.display(description="Transactions")
    def transaction_count(self, obj):
        return obj.transactions.count()


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "session",
        "event_type",
        "created_at",
    )

    list_filter = (
        "event_type",
        "created_at",
        "session__status",
    )

    search_fields = (
        "session__session_id",
    )

    autocomplete_fields = (
        "session",
    )

    readonly_fields = (
        "created_at",
    )

    ordering = (
        "-created_at",
    )