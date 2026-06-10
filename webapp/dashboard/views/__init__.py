"""
Dashboard views package.

Re-exports all views for backwards compatibility with urls.py.
"""

# Core views
from .core import (
    index,
    api_stations,
    api_demo,
    api_live,
    api_refresh,
    api_auth_status,
    logout_view,
)

# Landing page
from .landing import landing_page, privacy_page

# Feedback board views
from .feedback import (
    api_suggestions,
    api_suggestion_create,
    api_suggestion_vote,
    api_suggestion_detail,
    api_comment_create,
    api_suggestion_delete,
)

# Account/settings views
from .account import (
    settings_page,
    api_update_profile,
    api_downgrade_plan,
    api_delete_account,
)

# Health check
from .health import health_check

# Public API v1 data endpoints
from .api import api_v1_live, api_v1_stations, api_v1_cities, api_docs

# API key management
from .keys import api_create_key, api_revoke_key

# Billing & subscriptions
from .billing import (
    billing_page,
    api_create_payment,
    api_payment_webhook,
    api_subscription_status,
    api_test_upgrade,
)

# JWT token endpoints
from .tokens import api_v1_get_token, api_v1_refresh_token, api_v1_revoke_token

# ECCC model ingestion (isolated from the alert engine)
from .eccc import api_refresh_eccc, api_eccc

# Plan / Ontario smoke-plume tracker (isolated from the alert engine)
from .plan import api_plan_refresh, api_plan_frames, plan_page, api_plan_confirm

# Export all for `from dashboard.views import *`
__all__ = [
    # Landing
    "landing_page",
    "privacy_page",
    # Core
    "index",
    "api_stations",
    "api_demo",
    "api_live",
    "api_refresh",
    "api_auth_status",
    "logout_view",
    # Feedback
    "api_suggestions",
    "api_suggestion_create",
    "api_suggestion_vote",
    "api_suggestion_detail",
    "api_comment_create",
    "api_suggestion_delete",
    # Account
    "settings_page",
    "api_update_profile",
    "api_downgrade_plan",
    "api_delete_account",
    # Health
    "health_check",
    # Public API v1
    "api_v1_live",
    "api_v1_stations",
    "api_v1_cities",
    "api_docs",
    "api_create_key",
    "api_revoke_key",
    # Subscriptions
    "api_create_payment",
    "api_payment_webhook",
    "api_subscription_status",
    "api_test_upgrade",
    "billing_page",
    # JWT auth
    "api_v1_get_token",
    "api_v1_refresh_token",
    "api_v1_revoke_token",
    # ECCC model ingestion
    "api_refresh_eccc",
    "api_eccc",
    # Plan / plume tracker
    "api_plan_refresh",
    "api_plan_frames",
    "plan_page",
    "api_plan_confirm",
]
