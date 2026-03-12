"""
Management command: validate_saqs

Loads SAQS (Special Air Quality Statements) validation data and reports summary stats.
Data: data/LAPTOP TSF 2026/10. Validation/SAQS Validation (2015-2023)/

Full validation (comparing CLEAR alerts vs SAQS events by date) would require
historical CLEAR alert logs; this command provides SAQS baseline stats for reference.
"""

import csv
import os

from django.conf import settings
from django.core.management.base import BaseCommand


SAQS_PATH = os.path.join(
    settings.DATA_DIR,
    "LAPTOP TSF 2026",
    "10. Validation",
    "SAQS Validation (2015-2023)",
    "01.RAW_Data_SAQS",
    "RAW_ALL_Ontario_SAQS_Alerts.csv",
)


class Command(BaseCommand):
    help = "Load SAQS validation data and report summary stats (per CLEAR methodology validation)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--region",
            type=str,
            default=None,
            help="Filter to specific region (e.g. 'City of Toronto')",
        )

    def handle(self, *args, **options):
        if not os.path.isfile(SAQS_PATH):
            self.stderr.write(f"SAQS file not found: {SAQS_PATH}")
            return

        region_filter = options.get("region")

        with open(SAQS_PATH, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            rows = list(reader)

        if len(rows) < 3:
            self.stderr.write("SAQS file has insufficient rows")
            return

        # Row 0 = title, row 1 = header
        header = rows[1]
        year_cols = [c for c in header if c and str(c).strip().startswith("SAQS_")]
        self.stdout.write(f"Years: {year_cols}")
        self.stdout.write("")

        total_by_year = {col: 0 for col in year_cols}
        region_idx = 0
        for i, h in enumerate(header):
            if "region" in str(h).lower() or ("air" in str(h).lower() and "quality" in str(h).lower()):
                region_idx = i
                break

        for row in rows[2:]:  # Data starts at row 2
            if len(row) <= region_idx:
                continue
            region = row[region_idx].strip()
            if region_filter and region_filter.lower() not in region.lower():
                continue
            for j, col in enumerate(year_cols):
                try:
                    col_idx = header.index(col)
                except ValueError:
                    col_idx = j + 1
                if col_idx < len(row):
                    try:
                        total_by_year[col] += int(row[col_idx] or 0)
                    except ValueError:
                        pass

        self.stdout.write("SAQS alert counts by year (Ontario):")
        for col in sorted(year_cols):
            self.stdout.write(f"  {col}: {total_by_year.get(col, 0)}")
        self.stdout.write("")
        self.stdout.write("Reference: CLEAR methodology validation uses 33 events (2003-2023).")
        if region_filter:
            self.stdout.write(f"Filtered to region: {region_filter}")
