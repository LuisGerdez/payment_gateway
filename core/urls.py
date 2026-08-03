from django.urls import path

from .views import CheckoutSessionCreateView, CheckoutSessionDetailView, CheckoutSessionSyncView

urlpatterns = [
    path("sessions/", CheckoutSessionCreateView.as_view(), name="checkout-session-create"),
    path("sessions/<uuid:session_id>/", CheckoutSessionDetailView.as_view(), name="checkout-session-detail"),
    path("sessions/<uuid:session_id>/sync/", CheckoutSessionSyncView.as_view(), name="checkout-session-sync"),
]