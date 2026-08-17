import base64
import hashlib
import hmac
import json
import logging

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction as db_transaction

from .utils import validate_redsys_signature, call_paycomet_form, call_paycomet_payment_info, forward_webhook_notification
from .models import CheckoutSession, PaymentTransaction

logger = logging.getLogger(__name__)

from .utils import PAYCOMET_STATE_FAILED, PAYCOMET_STATE_PAID


def create_checkout_session(price, currency, success_url, cancel_url="", metadata=None, expires_at=None, original_ip="127.0.0.1"):
    """
    Creates a CheckoutSession in the DB, requests a hosted payment form from Paycomet,
    persists the returned payment URL, and logs a PaymentTransaction audit record.

    Args:
        price (Decimal): The amount for the checkout session.
        currency (str): The currency for the checkout session.
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
        currency=currency,
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

    paycomet_response = call_paycomet_form(
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
    PaymentTransaction.objects.create(session=checkout_session, event_type=PaymentTransaction.EventType.SESSION_CREATED, provider_response=paycomet_response)

    logger.info(
        "CheckoutSession %s created. Paycomet order=%s payment_url=%s",
        checkout_session.session_id,
        checkout_session.session_id,
        checkout_session.payment_url,
    )

    return checkout_session


def sync_session_from_paycomet(session_id, send_webhook=False):
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

    provider_data = call_paycomet_payment_info(str(session.session_id))

    # Payment result is nested inside provider_data["payment"]
    payment = provider_data.get("payment", {})
    paycomet_state = payment.get("state")  # 0=failed, 1=paid, 2=pending
    state_name = payment.get("stateName", "")
    event_type = PaymentTransaction.EventType.SESSION_QUERIED

    if paycomet_state == PAYCOMET_STATE_PAID and session.status == CheckoutSession.Status.PENDING:
        session.mark_as_paid()
        event_type = PaymentTransaction.EventType.PAYMENT_CONFIRMED
        logger.info("CheckoutSession %s confirmed as PAID via Paycomet sync (stateName=%s).", session.session_id, state_name)

    elif paycomet_state == PAYCOMET_STATE_FAILED and session.status == CheckoutSession.Status.PENDING:
        session.mark_as_failed()
        event_type = PaymentTransaction.EventType.PAYMENT_FAILED
        logger.info("CheckoutSession %s marked as FAILED via Paycomet sync (stateName=%s).", session.session_id, state_name)

    PaymentTransaction.objects.create(
        session=session,
        event_type=event_type,
        provider_response=provider_data,
    )

    if send_webhook:
        forward_webhook_notification(session)

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
            if not validate_redsys_signature(redsys_secret, params_b64, ds_order, ds_signature):
                logger.warning("Invalid Redsys signature for session %s", session_id)
                raise ValueError("Invalid payment callback signature.")

    # Log raw redirect params for audit
    PaymentTransaction.objects.create(
        session=session,
        event_type=PaymentTransaction.EventType.CALLBACK_PROCESSED,
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

    forward_webhook_notification(session)

    return session