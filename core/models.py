import uuid
from django.db import models
from django.utils import timezone


class CheckoutSession(models.Model):

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"

    session_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="EUR")
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    success_url = models.URLField(max_length=2048)
    cancel_url = models.URLField(max_length=2048, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    payment_url = models.URLField(max_length=2048, blank=True, default="")

    expires_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Checkout Session"
        verbose_name_plural = "Checkout Sessions"

    def __str__(self):
        return f"CheckoutSession {self.session_id} [{self.status}] {self.amount} {self.currency}"

    @property
    def is_expired(self):
        if self.expires_at and timezone.now() > self.expires_at:
            return True
        return False

    def mark_as_paid(self):
        if self.status != self.Status.PENDING:
            raise ValueError(f"Cannot mark as paid a session with status '{self.status}'.")
        if self.is_expired:
            raise ValueError("Cannot mark as paid an expired session.")
        self.status = self.Status.PAID
        self.paid_at = timezone.now()
        self.save(update_fields=["status", "paid_at", "updated_at"])

    def mark_as_expired(self):
        if self.status != self.Status.PENDING:
            raise ValueError(f"Cannot expire a session with status '{self.status}'.")
        self.status = self.Status.EXPIRED
        self.save(update_fields=["status", "updated_at"])

    def mark_as_failed(self):
        if self.status != self.Status.PENDING:
            raise ValueError(f"Cannot mark as failed a session with status '{self.status}'.")
        self.status = self.Status.FAILED
        self.save(update_fields=["status", "updated_at"])

    def cancel(self):
        if self.status != self.Status.PENDING:
            raise ValueError(f"Cannot cancel a session with status '{self.status}'.")
        self.status = self.Status.CANCELLED
        self.save(update_fields=["status", "updated_at"])


class PaymentTransaction(models.Model):
    """Audit log of every Paycomet interaction tied to a CheckoutSession."""

    class EventType(models.TextChoices):
        SESSION_CREATED = "session_created", "Session Created"
        SESSION_QUERIED = "session_queried", "Session Queried"
        WEBHOOK_RECEIVED = "webhook_received", "Webhook Received"
        CALLBACK_PROCESSED = "callback_processed", "Callback Processed"
        PAYMENT_CONFIRMED = "payment_confirmed", "Payment Confirmed"
        PAYMENT_FAILED = "payment_failed", "Payment Failed"

    session = models.ForeignKey(
        CheckoutSession,
        on_delete=models.CASCADE,
        related_name="transactions",
    )
    event_type = models.CharField(max_length=30, choices=EventType.choices, db_index=True)
    provider_response = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Payment Transaction"
        verbose_name_plural = "Payment Transactions"

    def __str__(self):
        return f"PaymentTransaction [{self.event_type}] session={self.session.session_id} @ {self.created_at}"
