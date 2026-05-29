"""
Billing, subscription, and payment views.
"""

import datetime
import hashlib
import hmac
import json
import logging
import os

import requests as http_requests

from django.conf import settings
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
        days = 365 if period == "yearly" else 30
        period_label = "12 months" if period == "yearly" else "30 days"

        api_key = getattr(settings, "NOWPAYMENTS_API_KEY", "")
        if not api_key:
            return JsonResponse({"error": "Payment system not configured"}, status=503)

        base_url = getattr(settings, "NOWPAYMENTS_API_URL", "https://api.nowpayments.io")
        resp = http_requests.post(
            f"{base_url}/v1/invoice",
            headers={
                "x-api-key": api_key,
                "Content-Type": "application/json",
            },
            json={
                "price_amount": amount,
                "price_currency": "usd",
                "order_id": f"{request.user.id}_{plan}_{period}_{int(timezone.now().timestamp())}",
                "order_description": f"CLEAR25 {plan.title()} Plan - {period_label}",
                "success_url": "https://clear25.xyz/dashboard/?tab=billing&status=success",
                "cancel_url": "https://clear25.xyz/dashboard/?tab=billing&status=cancelled",
                "ipn_callback_url": "https://clear25.xyz/api/v1/subscribe/webhook/",
            },
            timeout=15,
        )
        resp.raise_for_status()
        invoice = resp.json()

        # Save payment record
        Payment.objects.create(
            user=request.user,
            plan=plan,
            billing_period=period,
            amount_usd=amount,
            nowpayments_id=str(invoice.get("id", "")),
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
    """NOWPayments IPN callback — verifies and upgrades plan."""

    ipn_secret = getattr(settings, "NOWPAYMENTS_IPN_SECRET", "")
    if not ipn_secret:
        return JsonResponse({"error": "Not configured"}, status=503)

    # Verify HMAC signature
    sig = request.headers.get("x-nowpayments-sig", "")
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    # NOWPayments signature: HMAC-SHA512 of sorted JSON body
    sorted_body = json.dumps(body, sort_keys=True, separators=(",", ":"))
    expected_sig = hmac.new(
        ipn_secret.encode(), sorted_body.encode(), hashlib.sha512
    ).hexdigest()

    if not hmac.compare_digest(sig, expected_sig):
        return JsonResponse({"error": "Invalid signature"}, status=403)

    # Process payment
    payment_status = body.get("payment_status", "")
    invoice_id = str(body.get("invoice_id", "") or body.get("order_id", ""))

    if payment_status in ("finished", "confirmed"):
        try:
            payment = Payment.objects.get(nowpayments_id=invoice_id)
            payment.status = "confirmed"
            payment.save()

            # Upgrade user plan
            days = 365 if payment.billing_period == "yearly" else 30
            profile = payment.user.profile
            profile.plan = payment.plan
            profile.plan_expires = timezone.now() + datetime.timedelta(days=days)
            profile.save(update_fields=["plan", "plan_expires"])
        except Payment.DoesNotExist:
            pass

    elif payment_status in ("failed", "expired"):
        try:
            payment = Payment.objects.get(nowpayments_id=invoice_id)
            payment.status = "failed"
            payment.save()
        except Payment.DoesNotExist:
            pass

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
    """TEST ONLY: Simulate a plan upgrade without payment.

    Protected by CRON_SECRET. Usage:
      curl -X POST https://clear25.xyz/api/v1/subscribe/test/ \
           -H "Authorization: Bearer <CRON_SECRET>" \
           -H "Content-Type: application/json" \
           -d '{"user_id": 1, "plan": "pro"}'
    """
    cron_secret = os.environ.get("CRON_SECRET", "")
    auth_header = request.headers.get("Authorization", "")
    if not cron_secret or auth_header != f"Bearer {cron_secret}":
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
