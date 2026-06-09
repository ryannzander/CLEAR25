"""
Push notification service for CLEAR25.
Uses Apple Push Notification service (APNs) for iOS.
"""

import json
import os
import jwt
import time
import httpx
from django.conf import settings


# APNs configuration (set these in environment variables)
APNS_KEY_ID = os.environ.get("APNS_KEY_ID", "")
APNS_TEAM_ID = os.environ.get("APNS_TEAM_ID", "")
APNS_BUNDLE_ID = os.environ.get("APNS_BUNDLE_ID", "com.clear25.app")
APNS_KEY_PATH = os.environ.get("APNS_KEY_PATH", "")  # Path to .p8 file
APNS_KEY_CONTENT = os.environ.get("APNS_KEY_CONTENT", "")  # Or key content directly

# APNs endpoints
APNS_HOST_PROD = "https://api.push.apple.com"
APNS_HOST_DEV = "https://api.sandbox.push.apple.com"


def _get_apns_token():
    """Generate a JWT token for APNs authentication."""
    if not all([APNS_KEY_ID, APNS_TEAM_ID]):
        return None

    # Get the key content
    key_content = APNS_KEY_CONTENT
    if not key_content and APNS_KEY_PATH and os.path.exists(APNS_KEY_PATH):
        with open(APNS_KEY_PATH, "r") as f:
            key_content = f.read()

    if not key_content:
        return None

    headers = {
        "alg": "ES256",
        "kid": APNS_KEY_ID,
    }
    payload = {
        "iss": APNS_TEAM_ID,
        "iat": int(time.time()),
    }

    return jwt.encode(payload, key_content, algorithm="ES256", headers=headers)


def send_push_notification(device_token, title, body, data=None, badge=None, sound="default"):
    """
    Send a push notification to a single iOS device.

    Args:
        device_token: The APNs device token
        title: Notification title
        body: Notification body text
        data: Optional dict of custom data
        badge: Optional badge number
        sound: Sound name (default: "default")

    Returns:
        (success: bool, error: str or None)
    """
    token = _get_apns_token()
    if not token:
        return False, "APNs not configured"

    # Build the payload
    aps = {
        "alert": {
            "title": title,
            "body": body,
        },
        "sound": sound,
    }
    if badge is not None:
        aps["badge"] = badge

    payload = {"aps": aps}
    if data:
        payload.update(data)

    # Use production endpoint (change to DEV for testing)
    use_sandbox = os.environ.get("APNS_SANDBOX", "false").lower() == "true"
    host = APNS_HOST_DEV if use_sandbox else APNS_HOST_PROD
    url = f"{host}/3/device/{device_token}"

    headers = {
        "authorization": f"bearer {token}",
        "apns-topic": APNS_BUNDLE_ID,
        "apns-push-type": "alert",
        "apns-priority": "10",
    }

    try:
        with httpx.Client(http2=True) as client:
            response = client.post(
                url,
                headers=headers,
                json=payload,
                timeout=10.0,
            )

        if response.status_code == 200:
            return True, None
        elif response.status_code == 410:
            # Device token is no longer valid
            return False, "token_invalid"
        else:
            error_data = response.json() if response.content else {}
            return False, error_data.get("reason", f"HTTP {response.status_code}")
    except Exception as e:
        return False, str(e)


def send_alert_notifications(city, level_name, pm25_value, health_advisory):
    """
    Send push notifications to all devices subscribed to a city when an alert is triggered.

    Args:
        city: City name (e.g., "Toronto")
        level_name: Alert level (e.g., "HIGH", "VERY HIGH", "EXTREME")
        pm25_value: Predicted PM2.5 value
        health_advisory: Health advisory text
    """
    from .models import DeviceToken

    # Only send for significant alerts
    if level_name not in ["HIGH", "VERY HIGH", "EXTREME"]:
        return 0, 0

    # Get all active device tokens
    devices = DeviceToken.objects.filter(is_active=True)

    title = f"Air Quality Alert: {city}"
    # pm25_value may be None if a predicted value failed to compute; format defensively
    # so the notification text never raises instead of alerting.
    try:
        pm25_text = f"{float(pm25_value):.1f}"
    except (TypeError, ValueError):
        pm25_text = "—"
    body = f"{level_name} - PM2.5: {pm25_text} µg/m³"

    if level_name == "EXTREME":
        title = f"⚠️ EXTREME Air Quality: {city}"
    elif level_name == "VERY HIGH":
        title = f"🔴 Very High PM2.5: {city}"

    data = {
        "city": city,
        "level": level_name,
        "pm25": pm25_value,
        "type": "air_quality_alert",
    }

    sent = 0
    failed = 0
    tokens_to_deactivate = []

    for device in devices:
        # Check if device is subscribed to this city
        if device.cities and city not in device.cities:
            continue

        if device.platform == "ios":
            success, error = send_push_notification(
                device.token,
                title,
                body,
                data=data,
            )
            if success:
                sent += 1
            else:
                failed += 1
                if error == "token_invalid":
                    tokens_to_deactivate.append(device.token)

    # Deactivate invalid tokens
    if tokens_to_deactivate:
        DeviceToken.objects.filter(token__in=tokens_to_deactivate).update(is_active=False)

    return sent, failed
