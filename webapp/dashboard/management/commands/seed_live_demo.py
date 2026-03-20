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
        readings = services.get_all_demo_data()
        result = services.evaluate(stations, readings, previous_readings=readings)

        CachedResult.objects.update_or_create(
            key="latest",
            defaults={
                "results": result["stations"],
                "city_alerts": result["city_alerts"],
                "readings": readings,
            },
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"CachedResult updated: {len(result['stations'])} stations, "
                f"timestamp {timezone.now().isoformat()}"
            )
        )
