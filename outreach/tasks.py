import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from outreach.models import OutreachContact, WhatsAppMessage
from outreach.services import CostGuardRouter, process_webhook_payload

logger = logging.getLogger(__name__)


def get_templates_sent_last_24h(now=None) -> int:
    """Count business-initiated Cloud API utility templates sent in the rolling 24h window."""
    now = now or timezone.now()
    since = now - timedelta(hours=24)
    return WhatsAppMessage.objects.filter(
        direction=WhatsAppMessage.Direction.OUTBOUND,
        channel=WhatsAppMessage.Channel.CLOUD_API,
        message_type=WhatsAppMessage.MessageType.TEMPLATE,
        created_at__gte=since,
    ).exclude(delivery_status=WhatsAppMessage.DeliveryStatus.FAILED).count()


def get_remaining_tier_budget(now=None) -> int:
    """Return remaining Meta daily business-initiated messaging tier allowance."""
    limit = int(settings.WA_MESSAGING_TIER_DAILY_LIMIT)
    used = get_templates_sent_last_24h(now=now)
    return max(0, limit - used)


@shared_task(name="outreach.tasks.poll_due_drip_contacts")
def poll_due_drip_contacts() -> dict[str, int]:
    """
    Celery Beat task (every ~30s): claims a batch of due outreach contacts using
    row-level locking (`SELECT ... FOR UPDATE SKIP LOCKED`) and dispatches step execution.
    """
    now = timezone.now()
    batch_size = int(settings.WA_BATCH_SIZE)
    due_statuses = [
        OutreachContact.DripStatus.PENDING,
        OutreachContact.DripStatus.AWAITING_CONFIRMATION,
        OutreachContact.DripStatus.ACTIVE,
    ]

    claimed_ids: list[int] = []
    with transaction.atomic():
        due_qs = (
            OutreachContact.objects.select_for_update(skip_locked=True)
            .filter(
                is_opted_out=False,
                drip_status__in=due_statuses,
                next_send_at__isnull=False,
                next_send_at__lte=now,
            )
            .order_by("next_send_at")[:batch_size]
        )
        contacts = list(due_qs)
        if not contacts:
            return {"claimed": 0}

        # Lease claimed rows for 5 minutes so overlapping Beat ticks don't double-queue
        lease_until = now + timedelta(minutes=5)
        for contact in contacts:
            contact.next_send_at = lease_until
            claimed_ids.append(contact.id)

        OutreachContact.objects.bulk_update(contacts, ["next_send_at"])

        def _dispatch():
            for cid in claimed_ids:
                execute_drip_step_task.delay(cid)

        transaction.on_commit(_dispatch)

    logger.info("Poller claimed %d due contacts for drip execution.", len(claimed_ids))
    return {"claimed": len(claimed_ids)}


@shared_task(
    name="outreach.tasks.execute_drip_step_task",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def execute_drip_step_task(self, contact_id: int) -> dict:
    """Execute the Cost-Guard drip router for a single locked contact."""
    now = timezone.now()
    with transaction.atomic():
        contact = (
            OutreachContact.objects.select_for_update(skip_locked=True)
            .filter(pk=contact_id)
            .first()
        )
        if not contact:
            return {"action": "skipped", "reason": "not_found_or_locked"}

        # Enforce Meta Messaging Tier daily cap for paid Step 1 templates outside 24h window
        if (
            settings.WA_CLOUD_API_ENABLED
            and contact.drip_step == 1
            and not contact.is_in_service_window(now=now)
            and get_remaining_tier_budget(now=now) <= 0
        ):
            contact.next_send_at = now + timedelta(hours=1)
            contact.save(update_fields=["next_send_at", "updated_at"])
            logger.warning(
                "Daily Meta tier limit (%s) reached; deferred contact %s by 1h.",
                settings.WA_MESSAGING_TIER_DAILY_LIMIT,
                contact.phone_e164,
            )
            return {"action": "deferred_tier_limit"}

        router = CostGuardRouter()
        result = router.execute_drip_step(contact, now=now)
        # Convert Decimal for Celery JSON serialization
        if "cost_usd" in result:
            result["cost_usd"] = str(result["cost_usd"])
        return result


@shared_task(name="outreach.tasks.process_webhook_event_task")
def process_webhook_event_task(payload: dict) -> dict[str, int]:
    """Async Celery task to process Meta WhatsApp webhook payloads & download voice notes."""
    return process_webhook_payload(payload)
