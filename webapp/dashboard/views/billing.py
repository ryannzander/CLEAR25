"""
Billing, subscription, and payment views.
"""

import datetime
import hashlib
import hmac
import json
import logging
import os
from decimal import Decimal

import requests as http_requests

from django.conf import settings
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from ..models import Payment, PLAN_LIMITS
from .utils import safe_redirect, timing_safe_token_compare

logger = logging.getLogger(__name__)

PLAN_PRICES = {
    "pro":      {"monthly": 29,  "yearly": 290},   # yearly ≈ $24/mo, save 17%
    "business": {"monthly": 99,  "yearly": 948},   # yearly ≈ $79/mo, save 20%
}

# Payment statuses considered terminal — once confirmed, never re-extend.
_TERMINAL_OK = frozenset({"confirmed", "finished"})
_TERMINAL_FAIL = frozenset({"failed", "expired", "refunded"})


@ensure_csrf_cookie
def billing_page(request):
    """Render the billing/subscription page."""
    if not request.user.is_authenticated:
        return safe_redirect("/accounts/google/login/")
    try:
        profile = request.user.profile
        current_plan = profile.active_plan
        plan_expires = profile.plan_expires
    except Exception:
        current_plan = "free"
        plan_expires = None
    return render(request, "dashboard/billing.html", {
        "current_plan": current_plan,
        "plan_expires": plan_expires,
    })


@require_http_methods(["POST"])
def api_create_payment(request):
    """Create a NOWPayments invoice for a plan upgrade."""
    try:
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=401)

        try:
            data = json.loads(request.body) if request.body else {}
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        plan = data.get("plan", "")
        period = data.get("period", "monthly")
        if plan not in PLAN_PRICES:
            return JsonResponse({"error": f"Invalid plan. Choose: {', '.join(PLAN_PRICES.keys())}"}, status=400)
        if period not in ("monthly", "yearly"):
            return JsonResponse({"error": "Invalid period. Choose: monthly, yearly"}, status=400)

        amount = PLAN_PRICES[plan][period]
        period_label = "12 months" if period == "yearly" else "30 days"

        api_key = getattr(settings, "NOWPAYMENTS_API_KEY", "")
        if not api_key:
            return JsonResponse({"error": "Payment system not configured"}, status=503)

        base_url = getattr(settings, "NOWPAYMENTS_API_URL", "https://api.nowpayments.io")
        # Opaque order ID — do not leak user/plan identifiers to the payment provider.
        order_id = f"clr-{timezone.now().strftime('%Y%m%d%H%M%S')}-{os.urandom(6).hex()}"

        resp = http_requests.post(
            f"{base_url}/v1/invoice",
            headers={
                "x-api-key": api_key,
                "Content-Type": "application/json",
            },
            json={
                "price_amount": amount,
                "price_currency": "usd",
                "order_id": order_id,
                "order_description": f"CLEAR25 {plan.title()} Plan - {period_label}",
                "success_url": "https://clear25.xyz/dashboard/?tab=billing&status=success",
                "cancel_url": "https://clear25.xyz/dashboard/?tab=billing&status=cancelled",
                "ipn_callback_url": "https://clear25.xyz/api/v1/subscribe/webhook/",
            },
            timeout=15,
        )
        resp.raise_for_status()
        invoice = resp.json()

        # Save payment record — store both the NOWPayments invoice ID and our
        # internal order ID so the webhook can locate the record by either.
        Payment.objects.create(
            user=request.user,
            plan=plan,
            billing_period=period,
            amount_usd=Decimal(str(amount)),
            currency="usd",
            nowpayments_id=str(invoice.get("id", "")),
            order_id=order_id,
            status="waiting",
        )

        return JsonResponse({
            "invoice_url": invoice.get("invoice_url"),
            "invoice_id": invoice.get("id"),
        })
    except Exception:
        logger.exception("api_create_payment: unexpected error for user %s", request.user.id)
        return JsonResponse({"error": "Payment service unavailable. Please try again."}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def api_payment_webhook(request):
    """NOWPayments IPN callback — verifies, validates amount, and upgrades plan.

    Security properties:
      - HMAC-SHA512 signature verified with timing-safe compare.
      - Idempotent: a payment already in a terminal-success state is not extended.
      - Amount and currency must match the stored Payment, preventing
        underpayment from unlocking a higher tier.
      - Expiry is `max(current_expiry, now) + plan_days` so a late confirmation
        does not shorten a longer existing subscription.
    """
    ipn_secret = getattr(settings, "NOWPAYMENTS_IPN_SECRET", "")
    if not ipn_secret:
        return JsonResponse({"error": "Not configured"}, status=503)

    sig = request.headers.get("x-nowpayments-sig", "")
    raw_body = request.body
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    if not isinstance(body, dict):
        return JsonResponse({"error": "Invalid payload"}, status=400)

    # NOWPayments signature: HMAC-SHA512 of the body keys sorted alphabetically.
    sorted_body = json.dumps(body, sort_keys=True, separators=(",", ":"))
    expected_sig = hmac.new(
        ipn_secret.encode("utf-8"),
        sorted_body.encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()

    if not hmac.compare_digest(sig, expected_sig):
        logger.warning("api_payment_webhook: signature mismatch")
        return JsonResponse({"error": "Invalid signature"}, status=403)

    payment_status = str(body.get("payment_status", "")).lower()
    invoice_id = str(body.get("invoice_id", "") or "")
    order_id = str(body.get("order_id", "") or "")

    # Locate the Payment record. Prefer invoice_id (provider-issued); fall back
    # to order_id (ours). Reject if we can't find a record at all.
    payment = None
    if invoice_id:
        payment = Payment.objects.filter(nowpayments_id=invoice_id).first()
    if payment is None and order_id:
        payment = Payment.objects.filter(order_id=order_id).first()
    if payment is None:
        logger.warning(
            "api_payment_webhook: unknown payment invoice=%s order=%s",
            invoice_id, order_id,
        )
        # Acknowledge so NOWPayments doesn't retry forever, but do nothing.
        return JsonResponse({"ok": True, "noop": True})

    # Idempotency: once a payment is terminal, we never re-process it. A replayed
    # webhook body (or a legitimate duplicate from NOWPayments) becomes a no-op.
    if payment.status in _TERMINAL_OK:
        return JsonResponse({"ok": True, "noop": "already_processed"})
    if payment.status in _TERMINAL_FAIL and payment_status not in _TERMINAL_OK:
        return JsonResponse({"ok": True, "noop": "already_terminal"})

    if payment_status in _TERMINAL_OK:
        # Validate amount and currency before granting the plan.
        try:
            paid_amount = Decimal(str(body.get("price_amount", "0")))
            paid_currency = str(body.get("price_currency", "")).lower()
        except (TypeError, ValueError):
            logger.warning(
                "api_payment_webhook: malformed amount/currency for invoice=%s",
                invoice_id,
            )
            return JsonResponse({"error": "Invalid amount"}, status=400)

        expected_currency = (payment.currency or "usd").lower()
        if paid_currency != expected_currency:
            logger.warning(
                "api_payment_webhook: currency mismatch invoice=%s expected=%s got=%s",
                invoice_id, expected_currency, paid_currency,
            )
            payment.status = "rejected"
            payment.save(update_fields=["status"])
            return JsonResponse({"error": "Currency mismatch"}, status=400)

        if paid_amount < payment.amount_usd:
            logger.warning(
                "api_payment_webhook: underpayment invoice=%s expected=%s got=%s",
                invoice_id, payment.amount_usd, paid_amount,
            )
            payment.status = "underpaid"
            payment.save(update_fields=["status"])
            return JsonResponse({"error": "Underpayment"}, status=400)

        days = 365 if payment.billing_period == "yearly" else 30
        now = timezone.now()

        with transaction.atomic():
            # Re-fetch with row lock to guard against concurrent webhook delivery.
            locked = Payment.objects.select_for_update().get(pk=payment.pk)
            if locked.status in _TERMINAL_OK:
                return JsonResponse({"ok": True, "noop": "already_processed"})
            locked.status = "confirmed"
            locked.save(update_fields=["status"])

            profile = locked.user.profile
            base = profile.plan_expires if (profile.plan_expires and profile.plan_expires > now) else now
            profile.plan = locked.plan
            profile.plan_expires = base + datetime.timedelta(days=days)
            profile.save(update_fields=["plan", "plan_expires"])

        return JsonResponse({"ok": True})

    if payment_status in _TERMINAL_FAIL:
        payment.status = payment_status
        payment.save(update_fields=["status"])
        return JsonResponse({"ok": True})

    # Intermediate states (sending, confirming, etc.) — record but do not grant.
    if payment_status and payment.status != payment_status:
        payment.status = payment_status
        payment.save(update_fields=["status"])
    return JsonResponse({"ok": True})


@require_http_methods(["GET"])
def api_subscription_status(request):
    """Get current subscription status."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Authentication required"}, status=401)

    profile = request.user.profile
    plan = profile.active_plan
    limits = PLAN_LIMITS[plan]

    return JsonResponse({
        "plan": plan,
        "plan_expires": profile.plan_expires.isoformat() if profile.plan_expires else None,
        "rate_limit": limits["rate_limit"],
        "max_keys": limits["max_keys"],
    })


@csrf_exempt
@require_http_methods(["POST"])
def api_test_upgrade(request):
    """TEST ONLY: simulate a plan upgrade without payment.

    Refuses to run unless DEBUG=True. CRON_SECRET is still required as a second
    factor. If you really need this in a staging environment, run with
    DEBUG=true; never in production.
    """
    if not settings.DEBUG:
        return JsonResponse({"error": "Not found"}, status=404)

    cron_secret = os.environ.get("CRON_SECRET", "")
    auth_header = request.headers.get("Authorization", "")
    expected = f"Bearer {cron_secret}" if cron_secret else ""
    if not cron_secret or not timing_safe_token_compare(auth_header, expected):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    plan = data.get("plan", "")
    if plan not in ("pro", "business"):
        return JsonResponse({"error": "plan must be 'pro' or 'business'"}, status=400)

    user_id = data.get("user_id")
    if not user_id:
        return JsonResponse({"error": "user_id required"}, status=400)

    from django.contrib.auth.models import User
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return JsonResponse({"error": f"User {user_id} not found"}, status=404)

    profile = user.profile
    profile.plan = plan
    profile.plan_expires = timezone.now() + datetime.timedelta(days=30)
    profile.save(update_fields=["plan", "plan_expires"])

    return JsonResponse({
        "ok": True,
        "user": user.email or user.username,
        "plan": plan,
        "expires": profile.plan_expires.isoformat(),
    })
