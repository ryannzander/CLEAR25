import datetime
import hashlib
import secrets
from django.db import models, transaction
from django.db.models import F
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone


PLAN_CHOICES = [
    ("free", "Free"),
    ("pro", "Pro"),
    ("business", "Business"),
]

PLAN_LIMITS = {
    "free":     {"rate_limit": 100,   "max_keys": 1},
    "pro":      {"rate_limit": 1000,  "max_keys": 5},
    "business": {"rate_limit": 10000, "max_keys": 20},
}


class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    last_fetch_time = models.DateTimeField(null=True, blank=True)
    last_fetch_results = models.JSONField(null=True, blank=True)
    plan = models.CharField(max_length=10, choices=PLAN_CHOICES, default="free")
    plan_expires = models.DateTimeField(null=True, blank=True)

    @property
    def active_plan(self):
        if self.plan == "free":
            return "free"
        if self.plan_expires and self.plan_expires < timezone.now():
            return "free"  # Expired
        return self.plan

    @property
    def rate_limit(self):
        return PLAN_LIMITS[self.active_plan]["rate_limit"]

    @property
    def max_api_keys(self):
        return PLAN_LIMITS[self.active_plan]["max_keys"]

    def can_fetch(self):
        if self.last_fetch_time is None:
            return True
        return timezone.now() - self.last_fetch_time > datetime.timedelta(minutes=30)

    def minutes_until_fetch(self):
        if self.can_fetch():
            return 0
        elapsed = timezone.now() - self.last_fetch_time
        remaining = datetime.timedelta(minutes=30) - elapsed
        return max(0, int(remaining.total_seconds() / 60) + 1)

    def seconds_until_fetch(self):
        if self.can_fetch():
            return 0
        elapsed = timezone.now() - self.last_fetch_time
        remaining = datetime.timedelta(minutes=30) - elapsed
        return max(0, int(remaining.total_seconds()))


class APIKey(models.Model):
    """API key for public API access."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_keys")
    key = models.CharField(max_length=64, unique=True, db_index=True)
    name = models.CharField(max_length=100, blank=True)  # Optional label
    created_at = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    # Rate limiting (per key)
    requests_this_hour = models.IntegerField(default=0)
    hour_started = models.DateTimeField(null=True, blank=True)
    total_requests = models.IntegerField(default=0)

    def save(self, *args, **kwargs):
        if not self.key:
            self.key = secrets.token_hex(32)  # 64-char hex string
        super().save(*args, **kwargs)

    def get_rate_limit(self):
        """Get rate limit based on user's plan."""
        try:
            return self.user.profile.rate_limit
        except UserProfile.DoesNotExist:
            return PLAN_LIMITS["free"]["rate_limit"]

    def check_rate_limit(self):
        """Check and update rate limit atomically.

        Returns ``(allowed, remaining, reset_seconds)``.

        Concurrency model: a single SELECT … FOR UPDATE inside a transaction
        serializes parallel requests against the same key so the counter can
        never overshoot the limit. The previous implementation read the counter
        into Python, decided, then wrote — a classic TOCTOU race that allowed
        the limit to be exceeded by roughly the worker count under load.
        """
        now = timezone.now()
        rate_limit = self.get_rate_limit()

        with transaction.atomic():
            locked = type(self).objects.select_for_update().get(pk=self.pk)

            window_expired = (
                not locked.hour_started
                or (now - locked.hour_started).total_seconds() >= 3600
            )
            if window_expired:
                locked.hour_started = now
                locked.requests_this_hour = 0

            reset_seconds = int(3600 - (now - locked.hour_started).total_seconds())

            if locked.requests_this_hour >= rate_limit:
                # Persist the (possibly reset) window so a future call sees
                # the same state, then refuse.
                locked.save(update_fields=["requests_this_hour", "hour_started"])
                # Sync in-memory instance for the caller.
                self.hour_started = locked.hour_started
                self.requests_this_hour = locked.requests_this_hour
                return False, 0, reset_seconds

            locked.requests_this_hour = F("requests_this_hour") + 1
            locked.total_requests = F("total_requests") + 1
            locked.last_used = now
            locked.save(update_fields=[
                "requests_this_hour", "hour_started", "last_used", "total_requests",
            ])
            # Refresh to materialize the F() expressions and sync to self.
            locked.refresh_from_db(fields=[
                "requests_this_hour", "hour_started", "last_used", "total_requests",
            ])
            self.hour_started = locked.hour_started
            self.requests_this_hour = locked.requests_this_hour
            self.total_requests = locked.total_requests
            self.last_used = locked.last_used
            remaining = max(0, rate_limit - locked.requests_this_hour)
            return True, remaining, reset_seconds

    def __str__(self):
        return f"{self.name or 'API Key'} ({self.key[:8]}...)"


class ReadingSnapshot(models.Model):
    """Stores the most recent readings per city for Rule 2 (dual-station sustained check)."""
    city = models.CharField(max_length=50, unique=True)
    readings = models.JSONField(default=dict)
    timestamp = models.DateTimeField(auto_now=True)


class CachedResult(models.Model):
    """Stores the latest server-side refresh results. Single row (key='latest')."""
    key = models.CharField(max_length=20, unique=True, default="latest")
    results = models.JSONField(default=list)
    city_alerts = models.JSONField(default=dict)
    readings = models.JSONField(default=dict)
    timestamp = models.DateTimeField(auto_now=True)


class Suggestion(models.Model):
    """User suggestion/feedback for the improvement board."""
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name="suggestions")
    title = models.CharField(max_length=200)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=['-created_at']),  # For list sorting
            models.Index(fields=['author']),  # For user filtering
        ]

    def vote_score(self):
        return self.votes.filter(value=1).count() - self.votes.filter(value=-1).count()

    def comment_count(self):
        return self.comments.count()


class SuggestionVote(models.Model):
    """Upvote/downvote on a suggestion."""
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    suggestion = models.ForeignKey(Suggestion, on_delete=models.CASCADE, related_name="votes")
    value = models.SmallIntegerField()  # 1 = upvote, -1 = downvote

    class Meta:
        unique_together = ("user", "suggestion")
        indexes = [
            models.Index(fields=['suggestion', 'value']),  # For vote counting
            models.Index(fields=['user']),  # For user vote lookups
        ]


class Comment(models.Model):
    """Comment on a suggestion."""
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name="comments")
    suggestion = models.ForeignKey(Suggestion, on_delete=models.CASCADE, related_name="comments")
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=['suggestion']),  # For comment counting
            models.Index(fields=['created_at']),  # For sorting
        ]


class DeviceToken(models.Model):
    """Push notification device tokens for iOS/Android."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="devices", null=True, blank=True)
    token = models.CharField(max_length=255, unique=True, db_index=True)
    platform = models.CharField(max_length=10, default="ios")  # ios, android, web
    created_at = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    # Subscription preferences
    cities = models.JSONField(default=list)  # List of cities to get alerts for, empty = all

    class Meta:
        indexes = [
            models.Index(fields=['is_active', 'platform']),
        ]

    def __str__(self):
        return f"{self.platform}: {self.token[:20]}..."


BILLING_PERIOD_CHOICES = [("monthly", "Monthly"), ("yearly", "Yearly")]


class Payment(models.Model):
    """Tracks crypto payments via NOWPayments."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="payments")
    plan = models.CharField(max_length=10, choices=PLAN_CHOICES)
    billing_period = models.CharField(max_length=10, choices=BILLING_PERIOD_CHOICES, default="monthly")
    amount_usd = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=10, default="usd")
    nowpayments_id = models.CharField(max_length=100, unique=True)
    # Opaque internal order ID — does not embed user/plan info.
    order_id = models.CharField(max_length=64, unique=True, db_index=True, default="")
    status = models.CharField(max_length=20, default="waiting")  # waiting, confirming, confirmed, failed, rejected, underpaid
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.email} - {self.plan} - {self.status}"


class RefreshToken(models.Model):
    """Opaque refresh token for JWT rotation.

    The raw token is given to the client exactly once.  Only its SHA-256
    hash is persisted so a database leak cannot be used to forge tokens.

    Refresh tokens are bound to the API key that originated them. Revoking the
    API key cascade-deletes its refresh tokens (FK on_delete=CASCADE), so a
    stolen refresh token can never outlive the key it was minted for.
    """
    user       = models.ForeignKey(User, on_delete=models.CASCADE, related_name="refresh_tokens")
    # Nullable for legacy rows created before the binding was added; new rows
    # always set it.
    api_key    = models.ForeignKey(
        "APIKey", on_delete=models.CASCADE,
        related_name="refresh_tokens", null=True, blank=True,
    )
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    revoked    = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["user", "revoked"]),
        ]

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def create_for_api_key(cls, api_key):
        """Generate a new refresh token bound to ``api_key``.

        Returns ``(raw_token, instance)``.
        """
        from .jwt_auth import REFRESH_TOKEN_LIFETIME
        raw        = secrets.token_hex(32)          # 64-char hex, 256 bits
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        expires_at = timezone.now() + REFRESH_TOKEN_LIFETIME
        instance   = cls.objects.create(
            user=api_key.user,
            api_key=api_key,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        return raw, instance

    # ── Lookup ────────────────────────────────────────────────────────────────

    @classmethod
    def verify(cls, raw_token):
        """Return the ``RefreshToken`` if valid and not expired; else ``None``.

        A refresh token is rejected if its originating API key has been revoked
        or removed.
        """
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        try:
            rt = cls.objects.select_related("user", "api_key").get(
                token_hash=token_hash, revoked=False,
            )
        except cls.DoesNotExist:
            return None
        if rt.expires_at < timezone.now():
            return None
        # Refuse if the bound API key has been revoked.
        if rt.api_key_id is not None and not rt.api_key.is_active:
            return None
        return rt

    def __str__(self):
        return f"RefreshToken(user={self.user_id}, revoked={self.revoked})"


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)
