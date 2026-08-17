import base64
import calendar
import hashlib
import hmac
import json
import logging
import requests

from django.conf import settings

from Crypto.Cipher import DES3

from .models import CheckoutSession


logger = logging.getLogger(__name__)

# Paycomet URL
PAYCOMET_FORM_URL = "https://rest.paycomet.com/v1/form"
PAYCOMET_PAYMENTS_URL = "https://rest.paycomet.com/v1/payments"

# Paycomet state codes returned by /v1/payments/{order}/info (inside ["payment"]["state"])
# 0 = Failed, 1 = Correct/Paid, 2 = Unfinished/Pending
PAYCOMET_STATE_FAILED = 0
PAYCOMET_STATE_PAID = 1
PAYCOMET_STATE_PENDING = 2


def get_client_ip(request):
    """Extract the real client IP, respecting X-Forwarded-For when present."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "127.0.0.1")


def call_paycomet_form(order, amount, currency, success_url, cancel_url, original_ip, language="es"):
    """
    Call the Paycomet /v1/form endpoint to create a hosted payment form.

    :param order: Unique order reference (used as the Paycomet order ID).
    :param amount: Amount in cents as string, e.g. "1099" for 10.99 EUR.
    :param currency: ISO 4217 currency code, e.g. "EUR".
    :param success_url: URL Paycomet redirects to on success (urlOk).
    :param cancel_url: URL Paycomet redirects to on failure/cancel (urlKo).
    :param original_ip: IP address of the end user (required by Paycomet).
    :param language: Language code for the hosted form (default: "es").
    :return: Parsed JSON response dict from Paycomet.
    :raises requests.HTTPError: If Paycomet returns a non-2xx status.
    """
    payload = {
        "operationType": 1,
        "language": language,
        "payment": {
            "terminal": int(settings.PAYCOMET_TERMINAL) if settings.PAYCOMET_TERMINAL else None,
            "order": order,
            "amount": str(amount),
            "currency": currency,
            "secure": 1,
            "userInteraction": 1,
            "originalIp": original_ip,
            "urlOk": success_url,
            "urlKo": cancel_url,
        },
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "PAYCOMET-API-TOKEN": settings.PAYCOMET_API_TOKEN,
    }

    logger.debug("Calling Paycomet /v1/form for order=%s amount=%s %s", order, amount, currency)

    response = requests.post(PAYCOMET_FORM_URL, json=payload, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()


def call_paycomet_payment_info(order):
    """
    Query Paycomet /v1/payments/{order}/info for the current payment state.

    :param order: The order reference used when creating the form (session_id string).
    :return: Parsed JSON response dict from Paycomet.
    :raises requests.HTTPError: If Paycomet returns a non-2xx status.
    """
    url = f"{PAYCOMET_PAYMENTS_URL}/{order}/info"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "PAYCOMET-API-TOKEN": settings.PAYCOMET_API_TOKEN,
    }
    payload = {"terminal": int(settings.PAYCOMET_TERMINAL) if settings.PAYCOMET_TERMINAL else None}

    logger.debug("Querying Paycomet payment info for order=%s", order)

    response = requests.post(url, json=payload, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()


def forward_webhook_notification(session):
    """
    Forward a processed payment event to WEBHOOK_FORWARD_URL.

    Sends a JSON POST with session data and signs it with HMAC-SHA256
    using WEBHOOK_FORWARD_SECRET if configured (X-Webhook-Signature header),
    so the receiver can validate the payload's authenticity.
    Failures are logged but never raise — Paycomet must always get HTTP 200.
    """
    forward_url = getattr(settings, "WEBHOOK_FORWARD_URL", "")
    if not forward_url:
        return

    """ body_data = {
        "session_id": str(session.session_id),
        "event": event_type,
        "status": session.status,
        "amount": str(session.amount),
        "currency": session.currency,
        "metadata": session.metadata,
        "paid_at": session.paid_at.isoformat() if session.paid_at else None,
    } """

    body_data = session_to_stripe_format(session)
    body_data["paycomet"] = True

    logger.info("Webhook for session %s forwarded.", session.session_id)
    logger.info("Forwarded webhook payload: %s", body_data)

    body = json.dumps(body_data)
    headers = {"Content-Type": "application/json"}

    forward_secret = getattr(settings, "WEBHOOK_FORWARD_SECRET", "")
    if forward_secret:
        signature = hmac.new(
            forward_secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers["X-Webhook-Signature"] = signature

    try:
        resp = requests.post(forward_url, data=body, headers=headers, timeout=5)
        logger.info("Webhook forwarded to %s — HTTP %s", forward_url, resp.status_code)
    except Exception as exc:
        logger.error("Failed to forward webhook to %s: %s", forward_url, exc)


def validate_redsys_signature(redsys_secret_b64, ds_merchant_parameters_b64, ds_order, ds_signature):
    """
    Validate HMAC_SHA256_V1 signature from Redsys/Paycomet (Bizum payments).

    Algorithm:
    1. Decode the merchant secret from base64.
    2. Derive key: 3DES-CBC encrypt Ds_Order (zero-padded to multiple of 8 bytes, IV=0).
    3. HMAC-SHA256 of the Ds_MerchantParameters base64 string using the derived key.
    4. Base64url-encode (no padding) and compare with Ds_Signature.
    """
    try:
        secret = base64.b64decode(redsys_secret_b64)
        order_bytes = ds_order.encode("utf-8")
        pad_len = (8 - len(order_bytes) % 8) % 8
        if pad_len:
            order_bytes += b"\x00" * pad_len
        cipher = DES3.new(secret, DES3.MODE_CBC, b"\x00" * 8)
        derived_key = cipher.encrypt(order_bytes)
        mac = hmac.new(derived_key, ds_merchant_parameters_b64.encode("utf-8"), hashlib.sha256).digest()
        computed = base64.urlsafe_b64encode(mac).rstrip(b"=").decode("utf-8")
        received = ds_signature.rstrip("=")
        return hmac.compare_digest(computed, received)
    except Exception as exc:
        logger.error("Redsys signature validation error: %s", exc)
        return False


def session_to_stripe_format(session):
    """Build a Stripe-compatible response envelope from a CheckoutSession."""
    STATUS_MAP = {
        CheckoutSession.Status.PENDING: "open",
        CheckoutSession.Status.PAID: "complete",
        CheckoutSession.Status.FAILED: "expired",
        CheckoutSession.Status.EXPIRED: "expired",
        CheckoutSession.Status.CANCELLED: "expired",
    }
    stripe_status = STATUS_MAP.get(session.status, "open")

    is_paid = session.status == CheckoutSession.Status.PAID
    payment_status = "paid" if is_paid else "unpaid"
    amount_cents = int(session.amount * 100)
    created_ts = int(calendar.timegm(session.created_at.timetuple()))
    expires_at_ts = (int(calendar.timegm(session.expires_at.timetuple())) if session.expires_at else None)

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