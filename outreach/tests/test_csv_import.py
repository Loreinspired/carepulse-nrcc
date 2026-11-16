import io
from datetime import datetime, timezone as dt_timezone

from django.test import TestCase

from outreach.importer import bulk_import_contacts_csv
from outreach.models import OutreachContact


class BulkCsvImportTests(TestCase):
    def test_5000_contact_import_with_hourly_stagger_and_jitter(self):
        lines = ["first_name,phone,location_met,soul_winner_name"]
        # Generate 5,000 valid Nigerian mobile numbers across 0803... and 0814...
        for i in range(5000):
            phone = f"0803{i:07d}"
            lines.append(f"Soul{i},{phone},Fajuyi Park,TeamAdo")
        # Add 1 duplicate and 1 invalid phone to verify filtering
        lines.append("Duplicate,08030000001,Fajuyi Park,TeamAdo")
        lines.append("BadRow,12345,Oja Oba,TeamAdo")

        csv_data = "\n".join(lines)
        start_at = datetime(2026, 11, 15, 8, 0, 0, tzinfo=dt_timezone.utc)

        result = bulk_import_contacts_csv(
            io.StringIO(csv_data),
            max_per_hour=250,
            start_at=start_at,
            rng_seed=42,
        )

        self.assertEqual(result.total_rows, 5002)
        self.assertEqual(result.created_count, 5000)
        self.assertEqual(result.skipped_duplicate_count, 1)
        self.assertEqual(result.invalid_count, 1)
        self.assertEqual(OutreachContact.objects.count(), 5000)

        # Verify hourly bucketing: 5,000 / 250 = 20 hourly buckets (hours 0..19)
        scheduled_times = list(
            OutreachContact.objects.order_by("id").values_list("next_send_at", flat=True)
        )
        bucket_counts: dict[int, int] = {}
        for ts in scheduled_times:
            hour_offset = int((ts - start_at).total_seconds() // 3600)
            bucket_counts[hour_offset] = bucket_counts.get(hour_offset, 0) + 1

        self.assertEqual(len(bucket_counts), 20)
        for hour_idx in range(20):
            self.assertEqual(bucket_counts[hour_idx], 250)

        # Verify jitter exists inside the first hour (timestamps are not all identical)
        first_hour_unique = len(set(scheduled_times[:250]))
        self.assertGreater(first_hour_unique, 200)

    def test_legacy_csv_with_prayer_column_imports_and_ignores_it(self):
        csv_data = "first_name,phone,prayer_point\nTunde,08031234567,Career breakthrough\n"
        result = bulk_import_contacts_csv(io.StringIO(csv_data), max_per_hour=250, rng_seed=1)
        self.assertEqual(result.created_count, 1)
        contact = OutreachContact.objects.get(phone_e164="+2348031234567")
        self.assertFalse(hasattr(contact, "prayer_point"))
