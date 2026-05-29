"""Security hardening migration.

- Payment: add `currency` (default "usd") and unique `order_id` columns so the
  webhook can validate currency and locate records by an opaque, internal ID.
- RefreshToken: add a non-null `api_key` FK so a refresh token is bound to the
  API key that originated it, and revoking that key invalidates the token.
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0014_rename_dashboard_r_user_id_revoked_idx_dashboard_r_user_id_89a6d7_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="currency",
            field=models.CharField(default="usd", max_length=10),
        ),
        migrations.AddField(
            model_name="payment",
            name="order_id",
            field=models.CharField(db_index=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="refreshtoken",
            name="api_key",
            field=models.ForeignKey(
                null=True,
                blank=True,
                on_delete=models.deletion.CASCADE,
                to="dashboard.apikey",
                related_name="refresh_tokens",
            ),
        ),
        # Backfill order_id for legacy rows with their primary key so the unique
        # constraint below can be applied. New rows always supply their own.
        migrations.RunSQL(
            sql=(
                "UPDATE dashboard_payment SET order_id = 'legacy-' || id "
                "WHERE order_id = '' OR order_id IS NULL;"
            ),
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            model_name="payment",
            name="order_id",
            field=models.CharField(db_index=True, default="", max_length=64, unique=True),
        ),
    ]
