import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from outreach.utils import e164_to_wa_id


def generate_rsvp_token() -> str:
    """Generate a URL-safe, compact unique token for 1-tap Sunday RSVP links."""
    return secrets.token_urlsafe(10)


class OutreachContact(models.Model):
    """Represents a soul-winning / outreach prospect tracked across Mode A & Mode B."""

    class DripStatus(models.TextChoices):
        PENDING = "PENDING", "Pending Drip"
        AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION", "Awaiting Confirmation"
        ACTIVE = "ACTIVE", "Active (24h Free Window)"
        REPLIED = "REPLIED", "Replied / Engaged"
        MANUAL_FALLBACK = "MANUAL_FALLBACK", "Manual wa.me Queue"
        COMPLETED = "COMPLETED", "Drip Completed"
        OPTED_OUT = "OPTED_OUT", "Opted Out (STOP)"
        INVALID_NUMBER = "INVALID_NUMBER", "Invalid Number"

    class RsvpStatus(models.TextChoices):
        NONE = "NONE", "No RSVP Yet"
        ATTENDING = "ATTENDING", "Yes, Coming This Sunday"
        NEED_RIDE = "NEED_RIDE", "Coming — Need Free Ride"
        MAYBE = "MAYBE", "Might Come"
        PRAYER_ONLY = "PRAYER_ONLY", "Pray With Me (Can't Attend)"

    class CaptureSource(models.TextChoices):
        FIELD_FORM = "FIELD_FORM", "Field Soul-Winner Form"
        CSV_IMPORT = "CSV_IMPORT", "Bulk CSV Import"
        INBOUND_QR = "INBOUND_QR", "Inbound WhatsApp / QR"
        ADMIN = "ADMIN", "Admin Entry"

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    phone_raw = models.CharField(max_length=32, blank=True)
    phone_e164 = models.CharField(max_length=20, unique=True, db_index=True)
    wa_id = models.CharField(max_length=20, blank=True, db_index=True)

    location_met = models.CharField(
        max_length=150,
        blank=True,
        help_text="E.g., Fajuyi Park, Oja Oba, EKSU Gate, ABUAD Axis",
    )
    prayer_point = models.TextField(blank=True)
    soul_winner_name = models.CharField(max_length=120, blank=True)
    capture_source = models.CharField(
        max_length=24,
        choices=CaptureSource.choices,
        default=CaptureSource.FIELD_FORM,
    )

    # Drip Engine & 24h Cost-Guard State
    drip_step = models.PositiveSmallIntegerField(default=1)
    drip_status = models.CharField(
        max_length=32,
        choices=DripStatus.choices,
        default=DripStatus.PENDING,
        db_index=True,
    )
    next_send_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_inbound_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_outbound_at = models.DateTimeField(null=True, blank=True)
    is_opted_out = models.BooleanField(default=False, db_index=True)
    manual_sent_step = models.PositiveSmallIntegerField(default=0)
    assigned_volunteer = models.CharField(max_length=120, blank=True)

    # Mode B: Web RSVP & Prayer Sync
    rsvp_token = models.CharField(
        max_length=32,
        unique=True,
        db_index=True,
        default=generate_rsvp_token,
    )
    rsvp_status = models.CharField(
        max_length=24,
        choices=RsvpStatus.choices,
        default=RsvpStatus.NONE,
        db_index=True,
    )
    rsvp_pickup_landmark = models.CharField(max_length=150, blank=True)
    rsvp_updated_at = models.DateTimeField(null=True, blank=True)

    import_batch_id = models.CharField(max_length=64, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["drip_status", "next_send_at"], name="idx_drip_poll_due"),
            models.Index(fields=["drip_status", "drip_step"], name="idx_drip_status_step"),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} ({self.phone_e164})"

    def save(self, *args, **kwargs):
        if self.phone_e164 and not self.wa_id:
            self.wa_id = e164_to_wa_id(self.phone_e164)
        if not self.rsvp_token:
            self.rsvp_token = generate_rsvp_token()
        super().save(*args, **kwargs)

    @property
    def full_name(self) -> str:
        if self.last_name:
            return f"{self.first_name} {self.last_name}".strip()
        return self.first_name

    def is_in_service_window(self, now=None) -> bool:
        """
        Return True if contact sent an inbound message within the last 24 hours.
        Inside this window, interactive/free-form WhatsApp replies cost $0.00.
        """
        if not self.last_inbound_at:
            return False
        now = now or timezone.now()
        elapsed = now - self.last_inbound_at
        return timedelta(0) <= elapsed < timedelta(hours=24)

    @property
    def window_expires_at(self):
        if not self.last_inbound_at:
            return None
        return self.last_inbound_at + timedelta(hours=24)

    @property
    def window_remaining_label(self) -> str:
        if not self.last_inbound_at:
            return "No inbound yet"
        now = timezone.now()
        expires = self.window_expires_at
        if now >= expires:
            return "24h window closed"
        remaining = expires - now
        total_mins = int(remaining.total_seconds() // 60)
        hours, mins = divmod(total_mins, 60)
        return f"{hours}h {mins}m left ($0.00)"

    def get_rsvp_url(self, base_url: str | None = None) -> str:
        base = (base_url or settings.SITE_BASE_URL).rstrip("/")
        return f"{base}/r/{self.rsvp_token}/"


class WhatsAppMessage(models.Model):
    """Audit log of all inbound/outbound WhatsApp messages, templates, and voice notes."""

    class Direction(models.TextChoices):
        INBOUND = "INBOUND", "Inbound"
        OUTBOUND = "OUTBOUND", "Outbound"

    class Channel(models.TextChoices):
        CLOUD_API = "CLOUD_API", "Meta Cloud API"
        MANUAL_WAME = "MANUAL_WAME", "Manual 1-Tap wa.me"
        SYSTEM = "SYSTEM", "System / Web RSVP"

    class MessageType(models.TextChoices):
        TEXT = "TEXT", "Text"
        INTERACTIVE = "INTERACTIVE", "Interactive Buttons"
        TEMPLATE = "TEMPLATE", "Utility Template"
        AUDIO = "AUDIO", "Voice Note / Audio"
        IMAGE = "IMAGE", "Image"
        DOCUMENT = "DOCUMENT", "Document"
        BUTTON = "BUTTON", "Quick Reply Tap"
        UNKNOWN = "UNKNOWN", "Other"

    class DeliveryStatus(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        SENT = "SENT", "Sent"
        DELIVERED = "DELIVERED", "Delivered"
        READ = "READ", "Read"
        FAILED = "FAILED", "Failed"
        MANUAL_OPENED = "MANUAL_OPENED", "Opened in WhatsApp"
        RECEIVED = "RECEIVED", "Received"

    contact = models.ForeignKey(
        OutreachContact,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    direction = models.CharField(max_length=16, choices=Direction.choices, db_index=True)
    channel = models.CharField(
        max_length=24,
        choices=Channel.choices,
        default=Channel.CLOUD_API,
    )
    message_type = models.CharField(
        max_length=24,
        choices=MessageType.choices,
        default=MessageType.TEXT,
    )
    drip_step = models.PositiveSmallIntegerField(null=True, blank=True)
    wamid = models.CharField(max_length=160, unique=True, null=True, blank=True, db_index=True)
    body = models.TextField(blank=True)
    template_name = models.CharField(max_length=128, blank=True)
    payload_json = models.JSONField(default=dict, blank=True)

    delivery_status = models.CharField(
        max_length=24,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.QUEUED,
        db_index=True,
    )
    error_code = models.CharField(max_length=32, blank=True)
    error_message = models.TextField(blank=True)
    estimated_cost_usd = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=Decimal("0.0000"),
    )

    media_id = models.CharField(max_length=160, blank=True)
    media_mime_type = models.CharField(max_length=100, blank=True)
    media_file = models.FileField(
        upload_to="whatsapp_media/%Y/%m/",
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"[{self.direction}/{self.channel}] {self.contact.phone_e164} ({self.delivery_status})"


class FollowUpNote(models.Model):
    """Pastoral / volunteer follow-up notes recorded in the Coordinator Dashboard."""

    contact = models.ForeignKey(
        OutreachContact,
        on_delete=models.CASCADE,
        related_name="notes",
    )
    author_name = models.CharField(max_length=120, default="Follow-Up Team")
    note = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Note on {self.contact.first_name} by {self.author_name}"
