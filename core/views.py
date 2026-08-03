import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import CheckoutSession
from .serializers import CheckoutSessionSerializer, CreateCheckoutSessionSerializer
import requests

from .services import create_checkout_session, sync_session_from_paycomet

logger = logging.getLogger(__name__)


def _get_client_ip(request):
    """Extract the real client IP, respecting X-Forwarded-For when present."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "127.0.0.1")


class CheckoutSessionCreateView(APIView):
    """
    POST /api/sessions/
    Creates a Paycomet-backed checkout session.
    fcplus sends amount, urls and metadata; gets back session_id + payment_url.
    """

    def post(self, request):
        serializer = CreateCheckoutSessionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        original_ip = _get_client_ip(request)

        session = create_checkout_session(
            price=data["amount"],
            success_url=data["success_url"],
            cancel_url=data.get("cancel_url", ""),
            metadata=data.get("metadata", {}),
            expires_at=data.get("expires_at"),
            original_ip=original_ip,
        )

        return Response(
            CheckoutSessionSerializer(session).data,
            status=status.HTTP_201_CREATED,
        )


class CheckoutSessionDetailView(APIView):
    """
    GET /api/sessions/<session_id>/
    Returns the current status and details of a checkout session.
    fcplus uses this to know whether the user has paid.
    """

    def get(self, request, session_id):
        try:
            session = CheckoutSession.objects.get(pk=session_id)
        except (CheckoutSession.DoesNotExist, ValueError):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response(CheckoutSessionSerializer(session).data)


class CheckoutSessionSyncView(APIView):
    """
    POST /api/sessions/<session_id>/sync/
    Queries Paycomet for the live payment state, syncs the local session status,
    and returns the session data together with the raw Paycomet provider response.
    """

    def post(self, request, session_id):
        try:
            session, provider_data = sync_session_from_paycomet(session_id)
        except CheckoutSession.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        except requests.HTTPError as exc:
            logger.error("Paycomet sync failed for session %s: %s", session_id, exc)
            return Response(
                {"detail": "Provider error.", "provider_error": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        response_data = CheckoutSessionSerializer(session).data
        response_data["provider_status"] = provider_data
        return Response(response_data)
