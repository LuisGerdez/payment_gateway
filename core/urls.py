from django.urls import path

from .views import (
    CheckoutSessionCallbackKoView,
    CheckoutSessionCallbackOkView,
    CheckoutSessionCreateView,
    CheckoutSessionDetailView,
    CheckoutSessionSyncView,
    HealthCheckView,
    PaycometWebhookView,
)

urlpatterns = [
    path("sessions/", CheckoutSessionCreateView.as_view(), name="checkout-session-create"),
    path("sessions/<uuid:session_id>/", CheckoutSessionDetailView.as_view(), name="checkout-session-detail"),
    path("sessions/<uuid:session_id>/sync/", CheckoutSessionSyncView.as_view(), name="checkout-session-sync"),
    path("sessions/<uuid:session_id>/callback/ok/", CheckoutSessionCallbackOkView.as_view(), name="checkout-session-callback-ok"),
    path("sessions/<uuid:session_id>/callback/ko/", CheckoutSessionCallbackKoView.as_view(), name="checkout-session-callback-ko"),

    path("webhook/paycomet/", PaycometWebhookView.as_view(), name="paycomet-webhook"),
    path("health/", HealthCheckView.as_view(), name="health-check"),
]