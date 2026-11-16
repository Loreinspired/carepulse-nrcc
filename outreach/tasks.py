import logging
from datetime import datetime

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from outreach.models import OutreachContact
from outreach.services import (  # noqa: F401  (re-exported for views/tests)
    DRIP_RUNNING_STATUSES,
    SEND_LEASE,
    CostGuardRouter,
    get_remaining_tier_budget,
    get_templates_sent_last_24h,
    process_webhook_payload,
)

logger = logging.getLogger(__name__)


@shared_task(name="outreach.tasks.poll_due_drip_contacts")
def poll_due_drip_contacts() -> dict[str, int]:
    """
    Celery Beat task (every ~30s): claims a batch of due outreach contacts using
    row-level locking (`SELECT ... FOR UPDATE SKIP LOCKED`) and dispatches step execution.

    Each claimed contact gets a lease (`next_send_at = now + SEND_LEASE`). The lease value
    is passed to the task, which only runs if the contact still carries that exact lease,
    so overlapping Beat ticks or redelivered tasks never double-send.
    """
    now = timezone.now()
    batch_size = int(settings.WA_BATCH_SIZE)

    claimed: list[tuple[int, str]] = []
    with transaction.atomic():
        due_qs = (
            OutreachContact.objects.select_for_update(skip_locked=True)
            .filter(
                is_opted_out=False,
                drip_status__in=list(DRIP_RUNNING_STATUSES),
                next_send_at__isnull=False,
                next_send_at__lte=now,
            )
            .order_by("next_send_at")[:batch_size]
        )
        contacts = list(due_qs)
        if not contacts:
            return {"claimed": 0}

        lease_until = now + SEND_LEASE
        for contact in contacts:
            contact.next_send_at = lease_until
            claimed.append((contact.id, lease_until.isoformat()))

        OutreachContact.objects.bulk_update(contacts, ["next_send_at"])

        def _dispatch():
            for cid, lease in claimed:
                execute_drip_step_task.delay(cid, lease)

        transaction.on_commit(_dispatch)

    logger.info("Poller claimed %d due contacts for drip execution.", len(claimed))
    return {"claimed": len(claimed)}


@shared_task(name="outreach.tasks.execute_drip_step_task")
def execute_drip_step_task(contact_id: int, lease_token: str | None = None) -> dict:
    """
    Execute the Cost-Guard drip router for a single claimed contact.

    Not auto-retried: a retry after an ambiguous failure could re-send a message Meta
    already delivered. The router records failures and moves contacts to the manual queue.
    """
    lease = datetime.fromisoformat(lease_token) if lease_token else None
    result = CostGuardRouter().execute_drip_step(contact_id, lease_token=lease)
    # Convert Decimal for Celery JSON serialization
    if "cost_usd" in result:
        result["cost_usd"] = str(result["cost_usd"])
    return result


@shared_task(name="outreach.tasks.process_webhook_event_task")
def process_webhook_event_task(payload: dict) -> dict[str, int]:
    """Async Celery task to process Meta WhatsApp webhook payloads & download voice notes."""
    return process_webhook_payload(payload)
