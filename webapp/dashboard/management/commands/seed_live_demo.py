"""
Populate CachedResult (key=latest) with evaluated demo readings so /api/live/ shows data
without calling WAQI. Useful for local dev or staging when refresh cron is not configured.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from dashboard import services
from dashboard.models import CachedResult


class Command(BaseCommand):
    help = "Write demo evaluation results to CachedResult so /api/live/ returns data"

    def handle(self, *args, **options):
        stations = services.load_all_stations()
        prev = services.build_demo_previous_readings(stations)
        result = services.evaluate(
            stations,
            {},
            previous_readings=prev,
            per_city_readings=services.DEMO_DATA,
            default_pm=services.DEMO_DEFAULT_PM25,
        )

        readings_store = {}
        for st in stations:
            tc = st.get("target_city", "")
            sid = st["id"]
            pm = services.DEMO_DATA.get(tc, {}).get(sid, services.DEMO_DEFAULT_PM25)
            readings_store[f"{sid}|{tc}"] = pm

        CachedResult.objects.update_or_create(
            key="latest",
            defaults={
                "results": result["stations"],
                "city_alerts": result["city_alerts"],
                "readings": readings_store,
            },
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"CachedResult updated: {len(result['stations'])} stations, "
                f"timestamp {timezone.now().isoformat()}"
            )
        )
