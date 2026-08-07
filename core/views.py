import calendar
import logging
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from django.http import HttpResponse, HttpResponseRedirect
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import CheckoutSession
from .serializers import CheckoutSessionSerializer, CreateCheckoutSessionSerializer
from .services import create_checkout_session, handle_payment_callback, process_paycomet_webhook, sync_session_from_paycomet

logger = logging.getLogger(__name__)


def _session_to_stripe_format(session):
    """Build a Stripe-compatible response envelope from a CheckoutSession."""
    _STATUS_MAP = {
        CheckoutSession.Status.PENDING: "open",
        CheckoutSession.Status.PAID: "complete",
        CheckoutSession.Status.FAILED: "expired",
        CheckoutSession.Status.EXPIRED: "expired",
        CheckoutSession.Status.CANCELLED: "expired",
    }
    stripe_status = _STATUS_MAP.get(session.status, "open")
    is_paid = session.status == CheckoutSession.Status.PAID
    payment_status = "paid" if is_paid else "unpaid"
    amount_cents = int(session.amount * 100)
    created_ts = int(calendar.timegm(session.created_at.timetuple()))
    expires_at_ts = (
        int(calendar.timegm(session.expires_at.timetuple())) if session.expires_at else None
    )

    return {
        "success": True,
        "status": stripe_status,
        "session": {
            "id": str(session.session_id),
            "object": "checkout.session",
            "amount_subtotal": amount_cents,
            "amount_total": amount_cents,
            "cancel_url": session.cancel_url,
            "created": created_ts,
            "currency": session.currency.lower(),
            "expires_at": expires_at_ts,
            "metadata": session.metadata,
            "payment_status": payment_status,
            "status": stripe_status,
            "success_url": session.success_url,
            "url": session.payment_url,
        },
    }


class HealthCheckView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        return Response({"status": "ok"})


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

        response_value = {
            "success": True if session else False,
            "session_id": session.session_id,
            "url": session.payment_url,
        }

        return Response(
            response_value,
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

        return Response(_session_to_stripe_format(session))


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


class CheckoutSessionCallbackOkView(APIView):
    """
    GET /api/sessions/<session_id>/callback/ok/
    Paycomet redirects the user here after a successful payment.
    Validates the callback (Bizum HMAC signature if present), syncs the session
    status with Paycomet, then redirects the browser to fcplusapp's success_url.
    """

    def get(self, request, session_id):
        raw_params = request.GET.dict()
        session = None

        try:
            session = handle_payment_callback(session_id, raw_params)
        except CheckoutSession.DoesNotExist:
            logger.error("Callback ok: session %s not found", session_id)
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        except ValueError:
            # Redsys signature invalid — redirect to cancel URL as a precaution
            logger.warning("Callback ok: invalid Redsys signature for session %s", session_id)
            try:
                session = CheckoutSession.objects.get(pk=session_id)
            except CheckoutSession.DoesNotExist:
                return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
            
            url = session.cancel_url

            parsed = urlparse(url)
            query = parse_qs(parsed.query)

            query["session_id"] = session_id
            query["canceled_payment"] = "true"
            query["error"] = "invalid_signature"

            new_query = urlencode(query, doseq=True)

            redirect_url = urlunparse(parsed._replace(query=new_query))

            return HttpResponseRedirect(redirect_url)
        except requests.HTTPError as exc:
            # Paycomet sync failed — redirect anyway; fcplusapp can poll status later
            logger.error("Callback ok: Paycomet sync failed for session %s: %s", session_id, exc)
            if session is None:
                try:
                    session = CheckoutSession.objects.get(pk=session_id)
                except CheckoutSession.DoesNotExist:
                    return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
                
        url = session.success_url

        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        query["session_id"] = session_id
        query["success_payment"] = "true"

        new_query = urlencode(query, doseq=True)

        redirect_url = urlunparse(parsed._replace(query=new_query))

        return HttpResponseRedirect(redirect_url)


class CheckoutSessionCallbackKoView(APIView):
    """
    GET /api/sessions/<session_id>/callback/ko/
    Paycomet redirects the user here on payment failure or cancellation.
    Syncs session status and redirects the browser to fcplusapp's cancel_url.
    """

    def get(self, request, session_id):
        raw_params = request.GET.dict()
        session = None

        try:
            session = handle_payment_callback(session_id, raw_params)
        except CheckoutSession.DoesNotExist:
            logger.error("Callback ko: session %s not found", session_id)
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        except (ValueError, requests.HTTPError) as exc:
            logger.error("Callback ko: error for session %s: %s", session_id, exc)
            if session is None:
                try:
                    session = CheckoutSession.objects.get(pk=session_id)
                except CheckoutSession.DoesNotExist:
                    return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        target_url = session.cancel_url or session.success_url

        parsed = urlparse(target_url)
        query = parse_qs(parsed.query)

        query["session_id"] = session_id
        query["success_payment"] = "false"

        new_query = urlencode(query, doseq=True)

        redirect_url = urlunparse(parsed._replace(query=new_query))

        return HttpResponseRedirect(redirect_url)


@method_decorator(csrf_exempt, name="dispatch")
class PaycometWebhookView(APIView):
    """
    POST /api/webhook/paycomet/
    Server-to-server notification from Paycomet (application/x-www-form-urlencoded).
    Validates NotificationHash, updates session status, and returns HTTP 200.
    Configure this URL in the Paycomet terminal panel under "Notification URL".
    """

    def post(self, request):
        payload = request.data
        try:
            process_paycomet_webhook(payload)
        except CheckoutSession.DoesNotExist:
            logger.error("Webhook: session not found for Order=%s", payload.get("Order"))
        except ValueError as exc:
            logger.warning("Webhook: rejected — %s", exc)
            return HttpResponse("INVALID", status=400)
        except Exception as exc:
            logger.exception("Webhook: unexpected error — %s", exc)
            return HttpResponse("ERROR", status=500)
        return HttpResponse("OK", status=200)
