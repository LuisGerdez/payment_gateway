import logging
from decimal import ROUND_HALF_UP, Decimal

import requests
from django.conf import settings

from .models import CheckoutSession, PaymentTransaction

logger = logging.getLogger(__name__)

PAYCOMET_FORM_URL = "https://rest.paycomet.com/v1/form"


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

    paycomet_response = _call_paycomet_form(
        order=str(checkout_session.session_id),
        amount=amount_cents,
        currency=checkout_session.currency,
        success_url=success_url,
        cancel_url=cancel_url,
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
