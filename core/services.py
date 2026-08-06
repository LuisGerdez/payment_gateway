import base64
import hashlib
import hmac
import json
import logging
from decimal import ROUND_HALF_UP, Decimal

import requests
from Crypto.Cipher import DES3
from django.conf import settings
from django.db import transaction as db_transaction

from .models import CheckoutSession, PaymentTransaction

logger = logging.getLogger(__name__)

PAYCOMET_FORM_URL = "https://rest.paycomet.com/v1/form"
PAYCOMET_PAYMENTS_URL = "https://rest.paycomet.com/v1/payments"

# Paycomet state codes returned by /v1/payments/{order}/info (inside ["payment"]["state"])
# 0 = Failed, 1 = Correct/Paid, 2 = Unfinished/Pending
_PAYCOMET_STATE_FAILED = 0
_PAYCOMET_STATE_PAID = 1
_PAYCOMET_STATE_PENDING = 2


def _validate_redsys_signature(redsys_secret_b64, ds_merchant_parameters_b64, ds_order, ds_signature):
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


def _call_paycomet_form(order, amount, currency, success_url, cancel_url, original_ip, language="es"):
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
            "terminal": settings.PAYCOMET_TERMINAL,
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


def create_checkout_session(price, success_url, cancel_url="", metadata=None, expires_at=None, original_ip="127.0.0.1"):
    """
    Creates a CheckoutSession in the DB, requests a hosted payment form from Paycomet,
    persists the returned payment URL, and logs a PaymentTransaction audit record.

    Args:
        price (Decimal): The amount for the checkout session.
        success_url (str): The URL to redirect to upon successful payment.
        cancel_url (str, optional): URL to redirect on cancel. Defaults to "".
        metadata (dict, optional): Additional metadata. Defaults to None.
        expires_at (datetime, optional): Expiration datetime. Defaults to None.
        original_ip (str): End-user IP forwarded to Paycomet. Defaults to "127.0.0.1".

    Returns:
        CheckoutSession: The created and persisted session instance (payment_url is populated).

    Raises:
        requests.HTTPError: If Paycomet returns a non-2xx response.
    """
    if metadata is None:
        metadata = {}

    checkout_session = CheckoutSession.objects.create(
        amount=price,
        success_url=success_url,
        cancel_url=cancel_url,
        metadata=metadata,
        expires_at=expires_at,
        status=CheckoutSession.Status.PENDING,
    )

    # Paycomet expects amount in cents as string (e.g. "1099" for 10.99 EUR)
    amount_cents = str(
        (Decimal(str(checkout_session.amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )

    # Paycomet redirects the user to this service's callback endpoints,
    # which validate, sync status, and then redirect to fcplusapp's final URLs.
    callback_ok_url = f"{settings.BASE_URL}/api/sessions/{checkout_session.session_id}/callback/ok/"
    callback_ko_url = f"{settings.BASE_URL}/api/sessions/{checkout_session.session_id}/callback/ko/"

    paycomet_response = _call_paycomet_form(
        order=str(checkout_session.session_id),
        amount=amount_cents,
        currency=checkout_session.currency,
        success_url=callback_ok_url,
        cancel_url=callback_ko_url,
        original_ip=original_ip,
    )

    # Persist the Paycomet-hosted form URL on the session
    checkout_session.payment_url = paycomet_response.get("challengeUrl", "")
    checkout_session.save(update_fields=["payment_url", "updated_at"])

    # Audit log
    PaymentTransaction.objects.create(
        session=checkout_session,
        event_type=PaymentTransaction.EventType.SESSION_CREATED,
        provider_response=paycomet_response,
    )

    logger.info(
        "CheckoutSession %s created. Paycomet order=%s payment_url=%s",
        checkout_session.session_id,
        checkout_session.session_id,
        checkout_session.payment_url,
    )

    return checkout_session


def _call_paycomet_payment_info(order):
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
    payload = {"terminal": settings.PAYCOMET_TERMINAL}

    logger.debug("Querying Paycomet payment info for order=%s", order)

    response = requests.post(url, json=payload, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()


def sync_session_from_paycomet(session_id):
    """
    Queries Paycomet for the current payment state of a session, updates the local
    CheckoutSession if the status has changed, and logs a PaymentTransaction audit record.

    Args:
        session_id (UUID | str): The session primary key.

    Returns:
        tuple[CheckoutSession, dict]: The (possibly updated) session and the raw
        Paycomet response.

    Raises:
        CheckoutSession.DoesNotExist: If no session matches session_id.
        requests.HTTPError: If Paycomet returns a non-2xx response.
    """
    session = CheckoutSession.objects.get(pk=session_id)

    provider_data = _call_paycomet_payment_info(str(session.session_id))

    # Payment result is nested inside provider_data["payment"]
    payment = provider_data.get("payment", {})
    paycomet_state = payment.get("state")  # 0=failed, 1=paid, 2=pending
    state_name = payment.get("stateName", "")
    event_type = PaymentTransaction.EventType.WEBHOOK_RECEIVED

    if paycomet_state == _PAYCOMET_STATE_PAID and session.status == CheckoutSession.Status.PENDING:
        session.mark_as_paid()
        event_type = PaymentTransaction.EventType.PAYMENT_CONFIRMED
        logger.info("CheckoutSession %s confirmed as PAID via Paycomet sync (stateName=%s).", session.session_id, state_name)

    elif paycomet_state == _PAYCOMET_STATE_FAILED and session.status == CheckoutSession.Status.PENDING:
        session.mark_as_failed()
        event_type = PaymentTransaction.EventType.PAYMENT_FAILED
        logger.info("CheckoutSession %s marked as FAILED via Paycomet sync (stateName=%s).", session.session_id, state_name)

    PaymentTransaction.objects.create(
        session=session,
        event_type=event_type,
        provider_response=provider_data,
    )

    return session, provider_data


def handle_payment_callback(session_id, raw_params):
    """
    Process a browser redirect callback from Paycomet.

    For Bizum, validates the Redsys HMAC_SHA256_V1 signature when
    PAYCOMET_REDSYS_SECRET is configured. Always syncs with the Paycomet
    API for the authoritative payment status.

    Args:
        session_id: The session primary key (UUID).
        raw_params (dict): Flat dict of query params from the browser redirect.

    Returns:
        CheckoutSession: The (possibly updated) session.

    Raises:
        CheckoutSession.DoesNotExist: If the session is not found.
        ValueError: If Redsys signature validation fails.
        requests.HTTPError: If the Paycomet API sync fails.
    """
    session = CheckoutSession.objects.get(pk=session_id)

    # Bizum/Redsys signature validation
    if "Ds_MerchantParameters" in raw_params:
        redsys_secret = getattr(settings, "PAYCOMET_REDSYS_SECRET", "")
        if redsys_secret:
            params_b64 = raw_params["Ds_MerchantParameters"]
            ds_signature = raw_params.get("Ds_Signature", "")
            padded_b64 = params_b64 + "=" * ((4 - len(params_b64) % 4) % 4)
            decoded = json.loads(base64.b64decode(padded_b64).decode("utf-8"))
            ds_order = decoded.get("Ds_Order", "")
            if not _validate_redsys_signature(redsys_secret, params_b64, ds_order, ds_signature):
                logger.warning("Invalid Redsys signature for session %s", session_id)
                raise ValueError("Invalid payment callback signature.")

    # Log raw redirect params for audit
    PaymentTransaction.objects.create(
        session=session,
        event_type=PaymentTransaction.EventType.WEBHOOK_RECEIVED,
        provider_response={"source": "browser_redirect", "params": raw_params},
    )

    # Sync with Paycomet for authoritative status (also logs a PaymentTransaction)
    session, _ = sync_session_from_paycomet(session_id)
    return session


def process_paycomet_webhook(payload):
    """
    Process a server-to-server webhook notification from Paycomet.

    Validates NotificationHash using SHA-512 per Paycomet docs:
    SHA512(AccountCode + TpvID + TransactionType + Order + Amount + Currency + md5(password) + BankDateTime + Response)

    Configure PAYCOMET_WEBHOOK_SECRET with the terminal's product password
    from the Paycomet panel.

    Args:
        payload (dict): Parsed form fields from the webhook POST.

    Returns:
        CheckoutSession: The updated session.

    Raises:
        CheckoutSession.DoesNotExist: If Order doesn't match any session.
        ValueError: If NotificationHash validation fails.
    """
    order = payload.get("Order", "")
    response = payload.get("Response", "")

    webhook_secret = getattr(settings, "PAYCOMET_WEBHOOK_SECRET", "")
    if webhook_secret:
        account_code     = payload.get("AccountCode", "")
        tpv_id           = payload.get("TpvID", "")
        transaction_type = payload.get("TransactionType", "")
        amount           = payload.get("Amount", "")
        currency         = payload.get("Currency", "")
        bank_datetime    = payload.get("BankDateTime", "")
        md5_password     = hashlib.md5(webhook_secret.encode("utf-8")).hexdigest()
        expected = hashlib.sha512(
            f"{account_code}{tpv_id}{transaction_type}{order}{amount}{currency}{md5_password}{bank_datetime}{response}".encode("utf-8")
        ).hexdigest()
        received = payload.get("NotificationHash", "")
        if not hmac.compare_digest(expected.lower(), received.lower()):
            logger.warning("Paycomet webhook: invalid NotificationHash for Order=%s", order)
            raise ValueError("Invalid webhook signature.")

    with db_transaction.atomic():
        session = CheckoutSession.objects.select_for_update().get(pk=order)
        event_type = PaymentTransaction.EventType.WEBHOOK_RECEIVED

        if response == "OK" and session.status == CheckoutSession.Status.PENDING:
            session.mark_as_paid()
            event_type = PaymentTransaction.EventType.PAYMENT_CONFIRMED
            logger.info("CheckoutSession %s confirmed PAID via webhook.", session.session_id)
        elif response == "KO" and session.status == CheckoutSession.Status.PENDING:
            session.mark_as_failed()
            event_type = PaymentTransaction.EventType.PAYMENT_FAILED
            logger.info("CheckoutSession %s marked FAILED via webhook.", session.session_id)
        else:
            logger.info(
                "Webhook for session %s ignored — already in final state '%s' (response=%s).",
                session.session_id, session.status, response,
            )

        PaymentTransaction.objects.create(
            session=session,
            event_type=event_type,
            provider_response=dict(payload),
        )

    _forward_webhook_notification(session, event_type)
    return session


def _forward_webhook_notification(session, event_type):
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

    body_data = {
        "session_id": str(session.session_id),
        "event": event_type,
        "status": session.status,
        "amount": str(session.amount),
        "currency": session.currency,
        "metadata": session.metadata,
        "paid_at": session.paid_at.isoformat() if session.paid_at else None,
    }
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
