import csv
import io
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import IO, Any

from django.conf import settings
from django.utils import timezone

from outreach.models import OutreachContact, generate_rsvp_token
from outreach.utils import PhoneNormalizationError, e164_to_wa_id, normalize_nigeria_phone


@dataclass
class ImportErrorRow:
    row_number: int
    raw_phone: str
    name: str
    reason: str


@dataclass
class BulkImportResult:
    batch_id: str
    total_rows: int = 0
    created_count: int = 0
    skipped_duplicate_count: int = 0
    invalid_count: int = 0
    hours_spanned: float = 0.0
    errors: list[ImportErrorRow] = field(default_factory=list)
    preview_schedule: list[dict[str, Any]] = field(default_factory=list)


_COLUMN_ALIASES = {
    "first_name": ("first_name", "firstname", "first name", "name", "full_name", "fullname"),
    "last_name": ("last_name", "lastname", "last name", "surname"),
    "phone": ("phone", "phone_number", "phonenumber", "phone number", "whatsapp", "mobile", "tel", "telephone"),
    "location_met": ("location_met", "location", "area", "zone", "place", "place_met", "axis"),
    "soul_winner_name": ("soul_winner_name", "soul_winner", "soul winner", "volunteer", "evangelist", "captured_by"),
}


def _resolve_headers(fieldnames: list[str] | None) -> dict[str, str]:
    """Map canonical keys to actual CSV column names."""
    if not fieldnames:
        return {}
    lower_map = {col.strip().lower(): col for col in fieldnames if col}
    resolved = {}
    for canonical, aliases in _COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lower_map:
                resolved[canonical] = lower_map[alias]
                break
    return resolved


def compute_staggered_send_time(
    index: int,
    start_at: datetime,
    max_per_hour: int,
    rng: random.Random | None = None,
) -> datetime:
    """
    Compute a staggered `next_send_at` timestamp for contact `index` (0-based).
    Distributes `max_per_hour` contacts per 1-hour bucket with randomized jitter
    within the hour so Meta Cloud API rate limits and tier caps are respected.
    """
    safe_rate = max(1, int(max_per_hour))
    hour_bucket = index // safe_rate
    rng = rng or random
    jitter_seconds = rng.uniform(0.0, 3599.0)
    return start_at + timedelta(hours=hour_bucket, seconds=jitter_seconds)


def bulk_import_contacts_csv(
    file_obj: IO[str] | IO[bytes] | str,
    max_per_hour: int | None = None,
    start_at: datetime | None = None,
    default_location: str = "",
    default_soul_winner: str = "",
    rng_seed: int | None = None,
) -> BulkImportResult:
    """
    Parse and import up to 5,000+ outreach contacts from CSV with +234 normalization,
    deduplication, and hourly time-jitter staggering.
    """
    if isinstance(file_obj, str):
        text_stream = io.StringIO(file_obj)
    else:
        raw = file_obj.read()
        if isinstance(raw, bytes):
            text_stream = io.StringIO(raw.decode("utf-8-sig", errors="replace"))
        else:
            text_stream = io.StringIO(raw)

    reader = csv.DictReader(text_stream)
    header_map = _resolve_headers(reader.fieldnames)

    batch_id = f"csv-{uuid.uuid4().hex[:10]}"
    result = BulkImportResult(batch_id=batch_id)

    if "phone" not in header_map:
        result.errors.append(
            ImportErrorRow(
                row_number=1,
                raw_phone="",
                name="",
                reason="Missing required phone column (expected 'phone', 'phone_number', 'whatsapp', or 'mobile').",
            )
        )
        result.invalid_count = 1
        return result

    rate_per_hour = max(1, int(max_per_hour or settings.WA_MAX_PER_HOUR))
    base_time = start_at or timezone.now()
    rng = random.Random(rng_seed) if rng_seed is not None else random.Random()

    parsed_rows: list[dict[str, Any]] = []
    seen_phones_in_csv: set[str] = set()

    for idx, row in enumerate(reader, start=2):
        result.total_rows += 1
        raw_phone = (row.get(header_map["phone"]) or "").strip()
        raw_first = (row.get(header_map.get("first_name", ""), "") or "").strip()
        raw_last = (row.get(header_map.get("last_name", ""), "") or "").strip()

        # Split full name if only a combined 'name' column was provided without last_name
        if raw_first and not raw_last and " " in raw_first and header_map.get("first_name", "").lower() in {"name", "full_name", "fullname"}:
            parts = raw_first.split(None, 1)
            raw_first, raw_last = parts[0], parts[1]

        first_name = (raw_first or "Friend")[:100]
        last_name = raw_last[:100]

        try:
            phone_e164 = normalize_nigeria_phone(raw_phone)
        except PhoneNormalizationError as exc:
            result.invalid_count += 1
            if len(result.errors) < 100:
                result.errors.append(
                    ImportErrorRow(
                        row_number=idx,
                        raw_phone=raw_phone,
                        name=first_name,
                        reason=str(exc),
                    )
                )
            continue

        if phone_e164 in seen_phones_in_csv:
            result.skipped_duplicate_count += 1
            continue
        seen_phones_in_csv.add(phone_e164)

        location_met = (row.get(header_map.get("location_met", ""), "") or default_location or "").strip()[:150]
        soul_winner = (row.get(header_map.get("soul_winner_name", ""), "") or default_soul_winner or "").strip()[:120]

        parsed_rows.append(
            {
                "first_name": first_name,
                "last_name": last_name,
                "phone_raw": raw_phone[:32],
                "phone_e164": phone_e164,
                "wa_id": e164_to_wa_id(phone_e164),
                "location_met": location_met,
                "soul_winner_name": soul_winner,
            }
        )

    if not parsed_rows:
        return result

    # Filter out phones already present in database (chunked for SQLite/Postgres compatibility)
    existing_phones: set[str] = set()
    candidate_phones = [r["phone_e164"] for r in parsed_rows]
    chunk_size = 500
    for i in range(0, len(candidate_phones), chunk_size):
        chunk = candidate_phones[i : i + chunk_size]
        existing_phones.update(
            OutreachContact.objects.filter(phone_e164__in=chunk).values_list("phone_e164", flat=True)
        )

    initial_status = (
        OutreachContact.DripStatus.PENDING
        if settings.WA_CLOUD_API_ENABLED
        else OutreachContact.DripStatus.MANUAL_FALLBACK
    )

    contacts_to_create: list[OutreachContact] = []
    send_idx = 0
    max_scheduled_at = base_time

    for item in parsed_rows:
        if item["phone_e164"] in existing_phones:
            result.skipped_duplicate_count += 1
            continue

        scheduled_at = compute_staggered_send_time(
            index=send_idx,
            start_at=base_time,
            max_per_hour=rate_per_hour,
            rng=rng,
        )
        if scheduled_at > max_scheduled_at:
            max_scheduled_at = scheduled_at

        contact = OutreachContact(
            first_name=item["first_name"],
            last_name=item["last_name"],
            phone_raw=item["phone_raw"],
            phone_e164=item["phone_e164"],
            wa_id=item["wa_id"],
            location_met=item["location_met"],
            soul_winner_name=item["soul_winner_name"],
            capture_source=OutreachContact.CaptureSource.CSV_IMPORT,
            drip_step=1,
            drip_status=initial_status,
            next_send_at=scheduled_at,
            rsvp_token=generate_rsvp_token(),
            import_batch_id=batch_id,
        )
        contacts_to_create.append(contact)

        if len(result.preview_schedule) < 10:
            result.preview_schedule.append(
                {
                    "first_name": item["first_name"],
                    "phone_e164": item["phone_e164"],
                    "location_met": item["location_met"],
                    "next_send_at": scheduled_at,
                }
            )
        send_idx += 1

    if contacts_to_create:
        OutreachContact.objects.bulk_create(contacts_to_create, batch_size=500, ignore_conflicts=True)
        result.created_count = len(contacts_to_create)
        result.hours_spanned = round((max_scheduled_at - base_time).total_seconds() / 3600.0, 2)

    return result
