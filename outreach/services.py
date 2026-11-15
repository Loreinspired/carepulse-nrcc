import hashlib
import hmac
import logging
import mimetypes
from datetime import timedelta
from decimal import Decimal
from typing import Any

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from outreach.models import OutreachContact, WhatsAppMessage
from outreach.utils import build_wa_me_url, e164_to_wa_id, normalize_nigeria_phone

logger = logging.getLogger(__name__)

OPT_OUT_KEYWORDS = {"STOP", "UNSUBSCRIBE", "CANCEL", "OPTOUT", "OPT OUT", "STOP ALL"}

_MIME_EXT_OVERRIDES = {
    "audio/ogg": ".ogg",
    "audio/ogg; codecs=opus": ".ogg",
    "audio/opus": ".opus",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/amr": ".amr",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "application/pdf": ".pdf",
}

_SHARED_SESSION: requests.Session | None = None


def get_http_session() -> requests.Session:
    """
    Return a module-level requests.Session configured with persistent TLS
    connection pooling (20 pools, max 50 connections) and automatic retries
    for transient 429/5xx responses across 5,000+ sends.
    """
    global _SHARED_SESSION
    if _SHARED_SESSION is None:
        session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            pool_connections=20,
            pool_maxsize=50,
            max_retries=retry_strategy,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _SHARED_SESSION = session
    return _SHARED_SESSION


class MetaCloudAPIError(Exception):
    """Raised when Meta Graph API returns a non-2xx response."""

    def __init__(self, message: str, error_code: str = "", status_code: int = 0, payload: dict | None = None):
        super().__init__(message)
        self.error_code = str(error_code or "")
        self.status_code = status_code
        self.payload = payload or {}


class MetaWhatsAppClient:
    """Direct Meta WhatsApp Cloud API client (v22.0+/v26.0, zero BSP markup)."""

    def __init__(self, session: requests.Session | None = None):
        self.session = session or get_http_session()
        self.api_version = settings.WA_API_VERSION
        self.phone_number_id = settings.WA_PHONE_NUMBER_ID
        self.access_token = settings.WA_ACCESS_TOKEN

    @property
    def messages_url(self) -> str:
        return f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def post_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.phone_number_id or not self.access_token:
            raise MetaCloudAPIError(
                "WA_PHONE_NUMBER_ID or WA_ACCESS_TOKEN is not configured.",
                error_code="CONFIG_MISSING",
            )

        resp = self.session.post(
            self.messages_url,
            json=payload,
            headers=self._headers(),
            timeout=(5, 15),
        )
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}

        if resp.status_code >= 400:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            code = str(err.get("code", resp.status_code))
            msg = err.get("message") or resp.text or f"HTTP {resp.status_code}"
            raise MetaCloudAPIError(msg, error_code=code, status_code=resp.status_code, payload=data)

        return data

    def send_utility_template(
        self,
        to_wa_id: str,
        template_name: str,
        first_name: str,
        location_met: str,
        language_code: str | None = None,
    ) -> dict[str, Any]:
        """Send a Meta-approved Utility Confirmation template ($0.0067)."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_wa_id,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code or settings.WA_TEMPLATE_LANGUAGE},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": first_name or "Friend"},
                            {"type": "text", "text": location_met or "Ado-Ekiti"},
                            {"type": "text", "text": settings.CHURCH_SHORT_NAME},
                        ],
                    }
                ],
            },
        }
        return self.post_message(payload)

    def send_interactive_buttons(
        self,
        to_wa_id: str,
        body_text: str,
        buttons: list[tuple[str, str]],
        header_text: str = "",
        footer_text: str = "",
    ) -> dict[str, Any]:
        """Send a $0.00 Free-Form Interactive Reply Button message inside the 24h window."""
        interactive: dict[str, Any] = {
            "type": "button",
            "body": {"text": body_text[:1024]},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": btn_id[:256], "title": title[:20]}}
                    for btn_id, title in buttons[:3]
                ]
            },
        }
        if header_text:
            interactive["header"] = {"type": "text", "text": header_text[:60]}
        if footer_text:
            interactive["footer"] = {"text": footer_text[:60]}

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_wa_id,
            "type": "interactive",
            "interactive": interactive,
        }
        return self.post_message(payload)

    def send_text(self, to_wa_id: str, body_text: str, preview_url: bool = True) -> dict[str, Any]:
        """Send a $0.00 Free-Form Text message inside the 24h window."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_wa_id,
            "type": "text",
            "text": {
                "preview_url": preview_url,
                "body": body_text[:4096],
            },
        }
        return self.post_message(payload)

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        """Fetch binary media (e.g., WhatsApp voice note) via two-step Graph API lookup."""
        if not self.access_token:
            raise MetaCloudAPIError("WA_ACCESS_TOKEN missing for media download.", error_code="CONFIG_MISSING")

        meta_url = f"https://graph.facebook.com/{self.api_version}/{media_id}"
        headers = {"Authorization": f"Bearer {self.access_token}"}

        info_resp = self.session.get(meta_url, headers=headers, timeout=(5, 15))
        if info_resp.status_code >= 400:
            raise MetaCloudAPIError(
                f"Failed to retrieve media metadata for {media_id}: {info_resp.text}",
                status_code=info_resp.status_code,
            )
        info_data = info_resp.json()
        download_url = info_data.get("url")
        mime_type = info_data.get("mime_type", "application/octet-stream")
        if not download_url:
            raise MetaCloudAPIError(f"Media response missing download URL for {media_id}")

        bin_resp = self.session.get(download_url, headers=headers, timeout=(5, 30))
        if bin_resp.status_code >= 400:
            raise MetaCloudAPIError(
                f"Failed to download media bytes for {media_id}: HTTP {bin_resp.status_code}",
                status_code=bin_resp.status_code,
            )
        return bin_resp.content, mime_type


def build_step_message_content(contact: OutreachContact, step: int) -> dict[str, Any]:
    """
    Build personalized pastoral follow-up copy and interactive buttons for Steps 1-3.
    Used both by Mode A (Cloud API interactive messages) and Mode B (1-tap wa.me links).
    """
    name = contact.first_name or "Friend"
    location = contact.location_met or "Ado-Ekiti"
    church = settings.CHURCH_SHORT_NAME
    sunday_time = settings.CHURCH_SUNDAY_TIME
    rsvp_url = contact.get_rsvp_url()

    prayer_snippet = ""
    if contact.prayer_point:
        prayer_snippet = f'\n\nWe are standing in faith with you on your prayer request: "{contact.prayer_point}".'

    if step == 1:
        body = (
            f"Peace and grace to you, {name}! It was such a blessing meeting you around {location}. "
            f"Welcome to the {church} family.{prayer_snippet}\n\n"
            f"Tap below or use your personal care card anytime:\n{rsvp_url}"
        )
        buttons = [
            ("rsvp_sunday", "Coming This Sunday"),
            ("need_ride", "Need Free Ride"),
            ("prayer_update", "Update Prayer"),
        ]
        header = f"{church} Care Team"
    elif step == 2:
        body = (
            f"Hello {name}, checking in from {church}! We prayed specifically for you today"
            f"{' regarding: ' + contact.prayer_point if contact.prayer_point else ''}.\n\n"
            f"Remember Jeremiah 29:11 — God's plans for you in Ado-Ekiti are for peace and a bright future. "
            f"How are things going today? Reply with a text or voice note anytime.\n\n"
            f"Care & RSVP link: {rsvp_url}"
        )
        buttons = [
            ("rsvp_sunday", "I'll Be There Sunday"),
            ("prayer_update", "New Prayer Request"),
            ("need_ride", "Need Pickup Ride"),
        ]
        header = "Mid-Week Prayer Check-In"
    else:
        body = (
            f"Hi {name}! This Sunday ({sunday_time}) at {church}, we'd love to worship with you "
            f"and welcome you warmly from {location}.\n\n"
            f"Let us know in 1 tap if we should save a seat or arrange a free bus pickup:\n{rsvp_url}"
        )
        buttons = [
            ("rsvp_sunday", "Save My Seat"),
            ("need_ride", "Arrange Free Ride"),
            ("prayer_update", "Pray With Me"),
        ]
        header = "Sunday Service Invite"

    return {
        "header": header,
        "body": body,
        "footer": "Reply STOP anytime to pause messages",
        "buttons": buttons,
    }


def build_manual_wame_url(contact: OutreachContact, step: int | None = None) -> str:
    """Build a personalized 1-tap wa.me URL for Mode B volunteers."""
    active_step = step or max(1, min(contact.drip_step, 3))
    content = build_step_message_content(contact, active_step)
    return build_wa_me_url(contact.phone_e164, content["body"])


def build_field_handshake_url(contact: OutreachContact) -> tuple[str, str]:
    """
    Build the Instant Field QR / wa.me Inbound Handshake link.
    When the prospect scans this QR code in the field, they send a pre-filled
    message TO the church WhatsApp number, opening a $0.00 24h window immediately.
    """
    location = contact.location_met or "Ado-Ekiti"
    prefill = (
        f"Hi {settings.CHURCH_SHORT_NAME}! I'm {contact.first_name} "
        f"(met around {location}). Please connect with me and pray with me!"
    )
    url = build_wa_me_url(settings.CHURCH_WHATSAPP_NUMBER, prefill)
    return url, prefill


class CostGuardRouter:
    """
    Enforces the Hybrid Dual-Mode Cost-Guard state machine:
      1. If Cloud API disabled -> transition immediately to MANUAL_FALLBACK ($0.00).
      2. If inside 24h Customer Service Window -> send $0.00 Free-Form Interactive Buttons.
         Schedule Step 2 (or Step 3) for +22h to chain inside the same free 24h window.
      3. If outside 24h window & Step 1 -> send $0.0067 Utility Confirmation Template.
      4. If outside 24h window & Step 2 or 3 -> SILENT CONTACT CUTOFF -> MANUAL_FALLBACK ($0.00).
    """

    def __init__(self, client: MetaWhatsAppClient | None = None):
        self.client = client or MetaWhatsAppClient()

    def execute_drip_step(self, contact: OutreachContact, now=None) -> dict[str, Any]:
        now = now or timezone.now()

        if contact.is_opted_out or contact.drip_status in {
            OutreachContact.DripStatus.OPTED_OUT,
            OutreachContact.DripStatus.COMPLETED,
            OutreachContact.DripStatus.INVALID_NUMBER,
        }:
            return {"action": "skipped", "reason": "terminal_or_opted_out", "cost_usd": Decimal("0.0000")}

        # Mode B fallback when Cloud API is disabled (e.g., waiting for Meta verification)
        if not settings.WA_CLOUD_API_ENABLED:
            contact.drip_status = OutreachContact.DripStatus.MANUAL_FALLBACK
            contact.next_send_at = None
            contact.save(update_fields=["drip_status", "next_send_at", "updated_at"])
            return {"action": "manual_fallback", "reason": "cloud_api_disabled", "cost_usd": Decimal("0.0000")}

        to_wa_id = contact.wa_id or e164_to_wa_id(contact.phone_e164)
        in_window = contact.is_in_service_window(now=now)

        # Case 1: Inside 24h Customer Service Window -> $0.00 Free-Form Interactive Message
        if in_window:
            return self._send_freeform_step(contact, to_wa_id, contact.drip_step, now)

        # Case 2: Outside 24h Window & Step 1 -> $0.0067 Utility Confirmation Template
        if contact.drip_step == 1:
            return self._send_step1_utility_template(contact, to_wa_id, now)

        # Case 3: Outside 24h Window & Step 2 or Step 3 -> Silent Contact Cutoff ($0.00)
        contact.drip_status = OutreachContact.DripStatus.MANUAL_FALLBACK
        contact.next_send_at = None
        contact.save(update_fields=["drip_status", "next_send_at", "updated_at"])
        return {
            "action": "manual_fallback",
            "reason": "silent_contact_cutoff",
            "cost_usd": Decimal("0.0000"),
        }

    def _send_freeform_step(
        self,
        contact: OutreachContact,
        to_wa_id: str,
        step: int,
        now,
    ) -> dict[str, Any]:
        content = build_step_message_content(contact, step)
        try:
            resp = self.client.send_interactive_buttons(
                to_wa_id=to_wa_id,
                body_text=content["body"],
                buttons=content["buttons"],
                header_text=content["header"],
                footer_text=content["footer"],
            )
        except MetaCloudAPIError as exc:
            return self._handle_send_error(contact, exc, step, content["body"], Decimal("0.0000"))

        wamid = _extract_wamid(resp)
        WhatsAppMessage.objects.create(
            contact=contact,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            channel=WhatsAppMessage.Channel.CLOUD_API,
            message_type=WhatsAppMessage.MessageType.INTERACTIVE,
            drip_step=step,
            wamid=wamid,
            body=content["body"],
            payload_json=resp,
            delivery_status=WhatsAppMessage.DeliveryStatus.SENT,
            estimated_cost_usd=Decimal("0.0000"),
        )

        contact.last_outbound_at = now
        if step == 1:
            contact.drip_step = 2
            contact.drip_status = OutreachContact.DripStatus.ACTIVE
            # 22-Hour Window Chaining: fire Step 2 inside the same free 24h window
            contact.next_send_at = now + timedelta(hours=22)
        elif step == 2:
            contact.drip_step = 3
            contact.drip_status = OutreachContact.DripStatus.ACTIVE
            contact.next_send_at = now + timedelta(hours=22)
        else:
            contact.drip_step = 3
            contact.drip_status = OutreachContact.DripStatus.COMPLETED
            contact.next_send_at = None

        contact.save(
            update_fields=[
                "drip_step",
                "drip_status",
                "next_send_at",
                "last_outbound_at",
                "updated_at",
            ]
        )
        return {
            "action": "sent_interactive",
            "step": step,
            "wamid": wamid,
            "cost_usd": Decimal("0.0000"),
        }

    def _send_step1_utility_template(
        self,
        contact: OutreachContact,
        to_wa_id: str,
        now,
    ) -> dict[str, Any]:
        template_name = settings.WA_STEP1_UTILITY_TEMPLATE
        template_cost = Decimal(settings.WA_UTILITY_TEMPLATE_COST_USD)
        try:
            resp = self.client.send_utility_template(
                to_wa_id=to_wa_id,
                template_name=template_name,
                first_name=contact.first_name,
                location_met=contact.location_met or "Ado-Ekiti",
            )
        except MetaCloudAPIError as exc:
            return self._handle_send_error(contact, exc, 1, f"[Template: {template_name}]", template_cost)

        wamid = _extract_wamid(resp)
        WhatsAppMessage.objects.create(
            contact=contact,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            channel=WhatsAppMessage.Channel.CLOUD_API,
            message_type=WhatsAppMessage.MessageType.TEMPLATE,
            drip_step=1,
            wamid=wamid,
            template_name=template_name,
            body=(
                f"Hi {contact.first_name}, thank you for connecting with {settings.CHURCH_SHORT_NAME} "
                f"around {contact.location_met or 'Ado-Ekiti'}. Tap Confirm & Connect to receive your prayer follow-up."
            ),
            payload_json=resp,
            delivery_status=WhatsAppMessage.DeliveryStatus.SENT,
            estimated_cost_usd=template_cost,
        )

        # Move drip_step to 2 and schedule check at +24h:
        # - If they confirm/reply before +24h, webhook immediately sends Step 1 welcome ($0.00) & chains Step 2 at +22h.
        # - If they ignore for 24h, Step 2 hits Silent Contact Cutoff -> MANUAL_FALLBACK ($0.00).
        contact.drip_step = 2
        contact.drip_status = OutreachContact.DripStatus.AWAITING_CONFIRMATION
        contact.last_outbound_at = now
        contact.next_send_at = now + timedelta(hours=24)
        contact.save(
            update_fields=[
                "drip_step",
                "drip_status",
                "next_send_at",
                "last_outbound_at",
                "updated_at",
            ]
        )
        return {
            "action": "sent_utility_template",
            "step": 1,
            "template_name": template_name,
            "wamid": wamid,
            "cost_usd": template_cost,
        }

    def _handle_send_error(
        self,
        contact: OutreachContact,
        exc: MetaCloudAPIError,
        step: int,
        body: str,
        attempted_cost: Decimal,
    ) -> dict[str, Any]:
        logger.warning(
            "Meta Cloud API send error for %s (code=%s): %s",
            contact.phone_e164,
            exc.error_code,
            exc,
        )
        WhatsAppMessage.objects.create(
            contact=contact,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            channel=WhatsAppMessage.Channel.CLOUD_API,
            message_type=WhatsAppMessage.MessageType.TEXT,
            drip_step=step,
            body=body,
            payload_json=exc.payload,
            delivery_status=WhatsAppMessage.DeliveryStatus.FAILED,
            error_code=exc.error_code,
            error_message=str(exc),
            estimated_cost_usd=Decimal("0.0000"),
        )

        if exc.error_code in {"131026", "131051"}:
            contact.drip_status = OutreachContact.DripStatus.INVALID_NUMBER
        else:
            # 131049 (ecosystem limit), 130429 (rate limit), 131047, etc. -> Mode B fallback
            contact.drip_status = OutreachContact.DripStatus.MANUAL_FALLBACK

        contact.next_send_at = None
        contact.save(update_fields=["drip_status", "next_send_at", "updated_at"])
        return {
            "action": "fallback_on_error",
            "error_code": exc.error_code,
            "error": str(exc),
            "cost_usd": Decimal("0.0000"),
        }


def verify_meta_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """Verify X-Hub-Signature-256 header against WA_APP_SECRET."""
    app_secret = settings.WA_APP_SECRET
    if not app_secret:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        app_secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    received = signature_header.split("=", 1)[1].strip()
    return hmac.compare_digest(expected, received)


def process_webhook_payload(payload: dict[str, Any], client: MetaWhatsAppClient | None = None) -> dict[str, int]:
    """
    Process a Meta WhatsApp Cloud API webhook payload (inbound messages + delivery statuses).
    Safe against duplicate deliveries via wamid uniqueness checks.
    """
    client = client or MetaWhatsAppClient()
    router = CostGuardRouter(client=client)
    stats = {"messages_processed": 0, "statuses_updated": 0, "duplicates_skipped": 0}

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            # 1. Process delivery statuses (sent, delivered, read, failed)
            for status_obj in value.get("statuses", []):
                wamid = status_obj.get("id")
                status_str = (status_obj.get("status") or "").upper()
                if not wamid:
                    continue
                msg = WhatsAppMessage.objects.filter(wamid=wamid).first()
                if msg:
                    status_map = {
                        "SENT": WhatsAppMessage.DeliveryStatus.SENT,
                        "DELIVERED": WhatsAppMessage.DeliveryStatus.DELIVERED,
                        "READ": WhatsAppMessage.DeliveryStatus.READ,
                        "FAILED": WhatsAppMessage.DeliveryStatus.FAILED,
                    }
                    new_status = status_map.get(status_str)
                    if new_status:
                        msg.delivery_status = new_status
                        errors = status_obj.get("errors") or []
                        if errors and new_status == WhatsAppMessage.DeliveryStatus.FAILED:
                            msg.error_code = str(errors[0].get("code", ""))
                            msg.error_message = errors[0].get("title") or errors[0].get("message", "")
                        msg.save(update_fields=["delivery_status", "error_code", "error_message"])
                        stats["statuses_updated"] += 1

            # 2. Process inbound messages & button taps
            contacts_meta = {
                c.get("wa_id"): c.get("profile", {}).get("name", "")
                for c in value.get("contacts", [])
                if c.get("wa_id")
            }

            for msg_obj in value.get("messages", []):
                wamid = msg_obj.get("id")
                if wamid and WhatsAppMessage.objects.filter(wamid=wamid).exists():
                    stats["duplicates_skipped"] += 1
                    continue

                from_wa_id = msg_obj.get("from", "")
                if not from_wa_id:
                    continue

                contact = _get_or_create_inbound_contact(
                    from_wa_id=from_wa_id,
                    profile_name=contacts_meta.get(from_wa_id, ""),
                )
                _handle_inbound_message(contact, msg_obj, router, client)
                stats["messages_processed"] += 1

    return stats


def _get_or_create_inbound_contact(from_wa_id: str, profile_name: str = "") -> OutreachContact:
    """Resolve existing contact by wa_id / normalized E.164, or create an inbound QR contact."""
    contact = OutreachContact.objects.filter(wa_id=from_wa_id).first()
    if contact:
        return contact

    try:
        phone_e164 = normalize_nigeria_phone(f"+{from_wa_id.lstrip('+')}")
    except ValueError:
        phone_e164 = f"+{from_wa_id.lstrip('+')}"

    contact = OutreachContact.objects.filter(phone_e164=phone_e164).first()
    if contact:
        if not contact.wa_id:
            contact.wa_id = from_wa_id
            contact.save(update_fields=["wa_id", "updated_at"])
        return contact

    first_name = (profile_name.split()[0] if profile_name else "") or "Friend"
    last_name = " ".join(profile_name.split()[1:]) if profile_name and len(profile_name.split()) > 1 else ""
    return OutreachContact.objects.create(
        first_name=first_name[:100],
        last_name=last_name[:100],
        phone_raw=from_wa_id,
        phone_e164=phone_e164,
        wa_id=from_wa_id,
        capture_source=OutreachContact.CaptureSource.INBOUND_QR,
        drip_step=1,
        drip_status=OutreachContact.DripStatus.ACTIVE,
        last_inbound_at=timezone.now(),
    )


def _extract_inbound_text_and_type(msg_obj: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return (message_type, body_text, button_id, media_id)."""
    raw_type = msg_obj.get("type", "unknown")

    if raw_type == "text":
        return (
            WhatsAppMessage.MessageType.TEXT,
            msg_obj.get("text", {}).get("body", ""),
            "",
            "",
        )
    if raw_type == "button":
        btn = msg_obj.get("button", {})
        return (
            WhatsAppMessage.MessageType.BUTTON,
            btn.get("text", "") or btn.get("payload", ""),
            btn.get("payload", ""),
            "",
        )
    if raw_type == "interactive":
        inter = msg_obj.get("interactive", {})
        itype = inter.get("type")
        if itype == "button_reply":
            br = inter.get("button_reply", {})
            return (
                WhatsAppMessage.MessageType.BUTTON,
                br.get("title", ""),
                br.get("id", ""),
                "",
            )
        if itype == "list_reply":
            lr = inter.get("list_reply", {})
            return (
                WhatsAppMessage.MessageType.BUTTON,
                lr.get("title", ""),
                lr.get("id", ""),
                "",
            )
    if raw_type == "audio":
        audio = msg_obj.get("audio", {})
        return (
            WhatsAppMessage.MessageType.AUDIO,
            "[Voice Note]",
            "",
            audio.get("id", ""),
        )
    if raw_type == "image":
        img = msg_obj.get("image", {})
        return (
            WhatsAppMessage.MessageType.IMAGE,
            img.get("caption", "") or "[Image]",
            "",
            img.get("id", ""),
        )
    if raw_type == "document":
        doc = msg_obj.get("document", {})
        return (
            WhatsAppMessage.MessageType.DOCUMENT,
            doc.get("filename", "") or "[Document]",
            "",
            doc.get("id", ""),
        )
    return WhatsAppMessage.MessageType.UNKNOWN, f"[{raw_type}]", "", ""


def _handle_inbound_message(
    contact: OutreachContact,
    msg_obj: dict[str, Any],
    router: CostGuardRouter,
    client: MetaWhatsAppClient,
) -> None:
    now = timezone.now()
    wamid = msg_obj.get("id")
    msg_type, body_text, button_id, media_id = _extract_inbound_text_and_type(msg_obj)

    inbound_msg = WhatsAppMessage.objects.create(
        contact=contact,
        direction=WhatsAppMessage.Direction.INBOUND,
        channel=WhatsAppMessage.Channel.CLOUD_API,
        message_type=msg_type,
        wamid=wamid,
        body=body_text,
        payload_json=msg_obj,
        delivery_status=WhatsAppMessage.DeliveryStatus.RECEIVED,
        estimated_cost_usd=Decimal("0.0000"),
        media_id=media_id,
    )

    # Download audio / voice note locally so Coordinator Dashboard can play it
    if media_id and msg_type in {WhatsAppMessage.MessageType.AUDIO, WhatsAppMessage.MessageType.IMAGE}:
        _attach_downloaded_media(inbound_msg, media_id, client)

    # Check STOP / opt-out keywords
    normalized_upper = (body_text or "").strip().upper()
    if normalized_upper in OPT_OUT_KEYWORDS:
        contact.is_opted_out = True
        contact.drip_status = OutreachContact.DripStatus.OPTED_OUT
        contact.next_send_at = None
        contact.last_inbound_at = now
        contact.save(update_fields=["is_opted_out", "drip_status", "next_send_at", "last_inbound_at", "updated_at"])
        return

    # Open / refresh the 24-hour free Customer Service Window
    previous_status = contact.drip_status
    contact.last_inbound_at = now

    # Handle interactive RSVP quick buttons
    if button_id == "rsvp_sunday":
        contact.rsvp_status = OutreachContact.RsvpStatus.ATTENDING
        contact.rsvp_updated_at = now
    elif button_id == "need_ride":
        contact.rsvp_status = OutreachContact.RsvpStatus.NEED_RIDE
        contact.rsvp_updated_at = now

    # 22-Hour Window Chaining:
    # If prospect just confirmed Step 1 Utility Template (or scanned QR / replied on Step 1),
    # immediately send the warm Step 1 pastoral welcome ($0.00) and schedule Step 2 for +22h.
    if previous_status == OutreachContact.DripStatus.AWAITING_CONFIRMATION:
        contact.drip_step = 1
        contact.drip_status = OutreachContact.DripStatus.ACTIVE
        contact.save(
            update_fields=[
                "last_inbound_at",
                "drip_step",
                "drip_status",
                "rsvp_status",
                "rsvp_updated_at",
                "updated_at",
            ]
        )
        if settings.WA_CLOUD_API_ENABLED:
            router._send_freeform_step(contact, contact.wa_id or e164_to_wa_id(contact.phone_e164), 1, now)
        else:
            contact.drip_step = 2
            contact.next_send_at = now + timedelta(hours=22)
            contact.save(update_fields=["drip_step", "next_send_at", "updated_at"])
        return

    # Otherwise surface in the Coordinator Replied Inbox and keep window open
    contact.drip_status = OutreachContact.DripStatus.REPLIED
    contact.save(
        update_fields=[
            "last_inbound_at",
            "drip_status",
            "rsvp_status",
            "rsvp_updated_at",
            "updated_at",
        ]
    )

    # Auto-acknowledge RSVP button taps inside free 24h window when Cloud API is enabled
    if settings.WA_CLOUD_API_ENABLED and button_id in {"rsvp_sunday", "need_ride", "prayer_update"}:
        _send_button_acknowledgement(contact, button_id, client)


def _send_button_acknowledgement(contact: OutreachContact, button_id: str, client: MetaWhatsAppClient) -> None:
    to_wa_id = contact.wa_id or e164_to_wa_id(contact.phone_e164)
    rsvp_url = contact.get_rsvp_url()
    if button_id == "rsvp_sunday":
        ack_text = (
            f"Amen, {contact.first_name}! We have reserved a warm welcome for you this Sunday "
            f"({settings.CHURCH_SUNDAY_TIME}). Details & directions: {rsvp_url}"
        )
    elif button_id == "need_ride":
        ack_text = (
            f"Noted, {contact.first_name}! Our transport team in Ado-Ekiti will reach out. "
            f"Please tap here to enter your pickup bus stop (e.g., Fajuyi, EKSU Gate, Oja Oba): {rsvp_url}"
        )
    else:
        ack_text = (
            f"We are ready to pray with you, {contact.first_name}! Reply here with your prayer request "
            f"(text or voice note) or update it at: {rsvp_url}"
        )

    try:
        resp = client.send_text(to_wa_id, ack_text)
        WhatsAppMessage.objects.create(
            contact=contact,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            channel=WhatsAppMessage.Channel.CLOUD_API,
            message_type=WhatsAppMessage.MessageType.TEXT,
            wamid=_extract_wamid(resp),
            body=ack_text,
            payload_json=resp,
            delivery_status=WhatsAppMessage.DeliveryStatus.SENT,
            estimated_cost_usd=Decimal("0.0000"),
        )
    except MetaCloudAPIError as exc:
        logger.warning("Failed to send button ack to %s: %s", contact.phone_e164, exc)


def _attach_downloaded_media(
    msg_instance: WhatsAppMessage,
    media_id: str,
    client: MetaWhatsAppClient,
) -> None:
    try:
        content_bytes, mime_type = client.download_media(media_id)
    except MetaCloudAPIError as exc:
        logger.warning("Voice note/media download failed for media_id=%s: %s", media_id, exc)
        return

    base_mime = mime_type.split(";")[0].strip().lower()
    ext = _MIME_EXT_OVERRIDES.get(mime_type.lower()) or _MIME_EXT_OVERRIDES.get(base_mime) or mimetypes.guess_extension(base_mime) or ".bin"
    filename = f"wa_{media_id}{ext}"
    msg_instance.media_mime_type = mime_type
    msg_instance.media_file.save(filename, ContentFile(content_bytes), save=False)
    msg_instance.save(update_fields=["media_mime_type", "media_file"])


def _extract_wamid(resp: dict[str, Any]) -> str | None:
    messages = resp.get("messages") or []
    if messages and isinstance(messages, list):
        return messages[0].get("id")
    return None
