import csv
import json
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Q, Sum
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from outreach.forms import BulkImportForm, FieldCaptureForm, RsvpForm
from outreach.importer import bulk_import_contacts_csv
from outreach.models import FollowUpNote, OutreachContact, WhatsAppMessage
from outreach.services import (
    CostGuardRouter,
    MetaCloudAPIError,
    MetaWhatsAppClient,
    build_field_handshake_url,
    build_manual_wame_url,
    build_step_message_content,
    process_webhook_payload,
    verify_meta_webhook_signature,
)
from outreach.tasks import get_remaining_tier_budget, process_webhook_event_task
from outreach.utils import build_wa_me_url, e164_to_wa_id, generate_qr_svg

logger = logging.getLogger(__name__)


def _enrich_contact_for_ui(contact: OutreachContact, base_url: str) -> OutreachContact:
    """Attach computed Mode B / 24h window UI attributes onto a contact instance."""
    active_step = max(1, min(contact.drip_step, 3))
    step_content = build_step_message_content(contact, active_step)
    contact.ui_step = active_step
    contact.ui_preview_text = step_content["body"]
    contact.ui_wame_url = build_manual_wame_url(contact, step=active_step)
    contact.ui_in_window = contact.is_in_service_window()
    contact.ui_rsvp_url = contact.get_rsvp_url(base_url=base_url)
    return contact


def _build_dashboard_metrics() -> dict:
    now = timezone.now()
    window_cutoff = now - timedelta(hours=24)

    total_contacts = OutreachContact.objects.count()
    active_window_count = OutreachContact.objects.filter(
        last_inbound_at__gte=window_cutoff,
        is_opted_out=False,
    ).count()

    manual_statuses = [OutreachContact.DripStatus.MANUAL_FALLBACK]
    if not settings.WA_CLOUD_API_ENABLED:
        manual_statuses.append(OutreachContact.DripStatus.PENDING)

    manual_queue_count = OutreachContact.objects.filter(
        is_opted_out=False,
        drip_status__in=manual_statuses,
    ).count()

    replied_count = OutreachContact.objects.filter(
        is_opted_out=False,
        drip_status=OutreachContact.DripStatus.REPLIED,
    ).count()

    rsvp_confirmed_count = OutreachContact.objects.filter(
        rsvp_status__in=[
            OutreachContact.RsvpStatus.ATTENDING,
            OutreachContact.RsvpStatus.NEED_RIDE,
        ]
    ).count()

    total_spend = (
        WhatsAppMessage.objects.aggregate(total=Sum("estimated_cost_usd"))["total"]
        or Decimal("0.0000")
    )

    return {
        "total_contacts": total_contacts,
        "active_window_count": active_window_count,
        "manual_queue_count": manual_queue_count,
        "replied_count": replied_count,
        "rsvp_confirmed_count": rsvp_confirmed_count,
        "total_spend_usd": total_spend,
        "daily_tier_limit": settings.WA_MESSAGING_TIER_DAILY_LIMIT,
        "remaining_tier_budget": get_remaining_tier_budget(now=now),
    }


@require_http_methods(["GET", "POST"])
def field_capture_view(request):
    """15-second mobile capture form for soul-winners in Ado-Ekiti."""
    if request.method == "POST":
        form = FieldCaptureForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            phone_e164 = cd["phone"]
            soul_winner = cd.get("soul_winner_name", "").strip()
            if soul_winner:
                request.session["soul_winner_name"] = soul_winner

            initial_status = (
                OutreachContact.DripStatus.PENDING
                if settings.WA_CLOUD_API_ENABLED
                else OutreachContact.DripStatus.MANUAL_FALLBACK
            )
            # Give a 2-minute grace window for on-the-spot Field QR Handshake
            next_send = timezone.now() + timedelta(minutes=2)

            outcome = cd.get("outreach_outcome") or OutreachContact.OutreachOutcome.INVITED

            contact, created = OutreachContact.objects.get_or_create(
                phone_e164=phone_e164,
                defaults={
                    "first_name": cd["first_name"].strip(),
                    "last_name": cd.get("last_name", "").strip(),
                    "phone_raw": request.POST.get("phone", "").strip()[:32],
                    "wa_id": e164_to_wa_id(phone_e164),
                    "outreach_outcome": outcome,
                    "location_met": cd.get("location_met", "").strip(),
                    "soul_winner_name": soul_winner,
                    "capture_source": OutreachContact.CaptureSource.FIELD_FORM,
                    "drip_step": 1,
                    "drip_status": initial_status,
                    "next_send_at": next_send,
                },
            )
            if not created:
                contact.outreach_outcome = outcome
                if cd.get("location_met"):
                    contact.location_met = cd["location_met"].strip()
                if soul_winner:
                    contact.soul_winner_name = soul_winner
                contact.save(
                    update_fields=[
                        "outreach_outcome",
                        "location_met",
                        "soul_winner_name",
                        "updated_at",
                    ]
                )

            return redirect("outreach:field_qr_handshake", pk=contact.pk)
    else:
        form = FieldCaptureForm(
            initial={
                "soul_winner_name": request.session.get("soul_winner_name", ""),
                "outreach_outcome": OutreachContact.OutreachOutcome.INVITED,
            }
        )

    return render(request, "outreach/field_capture.html", {"form": form})


@require_GET
def field_qr_handshake_view(request, pk: int):
    """Instant Field QR / wa.me Inbound Handshake screen after logging a prospect."""
    contact = get_object_or_404(OutreachContact, pk=pk)
    requested_outcome = request.GET.get("outcome")
    if requested_outcome in {
        OutreachContact.OutreachOutcome.INVITED,
        OutreachContact.OutreachOutcome.GOT_SAVED,
        OutreachContact.OutreachOutcome.FILLED_HOLY_GHOST,
    } and requested_outcome != contact.outreach_outcome:
        contact.outreach_outcome = requested_outcome
        contact.save(update_fields=["outreach_outcome", "updated_at"])

    base_url = request.build_absolute_uri("/").rstrip("/")
    handshake_url, prefill_text = build_field_handshake_url(contact)
    qr_svg = generate_qr_svg(handshake_url)

    return render(
        request,
        "outreach/field_qr_handshake.html",
        {
            "contact": contact,
            "handshake_url": handshake_url,
            "prefill_text": prefill_text,
            "qr_svg": qr_svg,
            "rsvp_url": contact.get_rsvp_url(base_url=base_url),
            "manual_wame_url": build_manual_wame_url(contact, step=1),
        },
    )


@require_GET
def dashboard_view(request):
    """Coordinator dashboard: Mode B 1-Tap wa.me Queue, Replied Inbox, Audio Player, & RSVPs."""
    tab = request.GET.get("tab", "queue")
    query = (request.GET.get("q") or "").strip()
    base_url = request.build_absolute_uri("/").rstrip("/")

    qs = OutreachContact.objects.prefetch_related("messages", "notes").order_by("-updated_at", "-created_at")

    if tab == "queue":
        queue_statuses = [OutreachContact.DripStatus.MANUAL_FALLBACK]
        if not settings.WA_CLOUD_API_ENABLED:
            queue_statuses.append(OutreachContact.DripStatus.PENDING)
        qs = qs.filter(is_opted_out=False, drip_status__in=queue_statuses)
    elif tab == "inbox":
        qs = qs.filter(
            Q(drip_status=OutreachContact.DripStatus.REPLIED)
            | Q(messages__direction=WhatsAppMessage.Direction.INBOUND)
        ).distinct()
    elif tab == "rsvp":
        qs = qs.exclude(rsvp_status=OutreachContact.RsvpStatus.NONE)

    if query:
        qs = qs.filter(
            Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(phone_e164__icontains=query)
            | Q(location_met__icontains=query)
            | Q(prayer_point__icontains=query)
        )

    contacts = [_enrich_contact_for_ui(c, base_url) for c in qs[:100]]
    metrics = _build_dashboard_metrics()

    is_htmx = request.headers.get("HX-Request") == "true"
    context = {
        "contacts": contacts,
        "tab": tab,
        "query": query,
        "metrics": metrics,
        "is_htmx": is_htmx,
    }
    if is_htmx:
        return render(request, "outreach/partials/dashboard_list.html", context)
    return render(request, "outreach/dashboard.html", context)


@require_POST
def mark_manual_sent_view(request, pk: int):
    """HTMX endpoint: record a Mode B 1-Tap wa.me send and advance drip step at $0.00."""
    contact = get_object_or_404(OutreachContact, pk=pk)
    now = timezone.now()
    step = max(1, min(contact.drip_step, 3))
    content = build_step_message_content(contact, step)

    WhatsAppMessage.objects.create(
        contact=contact,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        channel=WhatsAppMessage.Channel.MANUAL_WAME,
        message_type=WhatsAppMessage.MessageType.TEXT,
        drip_step=step,
        body=content["body"],
        delivery_status=WhatsAppMessage.DeliveryStatus.MANUAL_OPENED,
        estimated_cost_usd=Decimal("0.0000"),
    )

    contact.manual_sent_step = step
    contact.last_outbound_at = now
    if step < 3:
        contact.drip_step = step + 1
        contact.drip_status = OutreachContact.DripStatus.MANUAL_FALLBACK
    else:
        contact.drip_status = OutreachContact.DripStatus.COMPLETED
        contact.next_send_at = None

    contact.save(
        update_fields=[
            "manual_sent_step",
            "last_outbound_at",
            "drip_step",
            "drip_status",
            "next_send_at",
            "updated_at",
        ]
    )

    base_url = request.build_absolute_uri("/").rstrip("/")
    _enrich_contact_for_ui(contact, base_url)
    return render(
        request,
        "outreach/partials/contact_card.html",
        {"contact": contact, "feedback": f"Step {step} logged via 1-Tap wa.me ($0.00)!"},
    )


@require_POST
def coordinator_reply_view(request, pk: int):
    """HTMX endpoint: send a $0.00 Cloud API reply inside 24h window, or return wa.me fallback."""
    contact = get_object_or_404(OutreachContact, pk=pk)
    reply_text = (request.POST.get("reply_text") or "").strip()
    base_url = request.build_absolute_uri("/").rstrip("/")

    if not reply_text:
        _enrich_contact_for_ui(contact, base_url)
        return render(
            request,
            "outreach/partials/contact_card.html",
            {"contact": contact, "feedback": "Please type a reply message first."},
        )

    feedback = ""
    fallback_url = ""
    if settings.WA_CLOUD_API_ENABLED and contact.is_in_service_window():
        client = MetaWhatsAppClient()
        to_wa_id = contact.wa_id or e164_to_wa_id(contact.phone_e164)
        try:
            resp = client.send_text(to_wa_id, reply_text)
            wamid = (resp.get("messages") or [{}])[0].get("id")
            WhatsAppMessage.objects.create(
                contact=contact,
                direction=WhatsAppMessage.Direction.OUTBOUND,
                channel=WhatsAppMessage.Channel.CLOUD_API,
                message_type=WhatsAppMessage.MessageType.TEXT,
                wamid=wamid,
                body=reply_text,
                payload_json=resp,
                delivery_status=WhatsAppMessage.DeliveryStatus.SENT,
                estimated_cost_usd=Decimal("0.0000"),
            )
            contact.last_outbound_at = timezone.now()
            contact.save(update_fields=["last_outbound_at", "updated_at"])
            feedback = "Reply sent via Cloud API ($0.00 free window)."
        except MetaCloudAPIError as exc:
            fallback_url = build_wa_me_url(contact.phone_e164, reply_text)
            feedback = f"Cloud API error ({exc.error_code}); use 1-Tap wa.me link below."
    else:
        fallback_url = build_wa_me_url(contact.phone_e164, reply_text)
        WhatsAppMessage.objects.create(
            contact=contact,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            channel=WhatsAppMessage.Channel.MANUAL_WAME,
            message_type=WhatsAppMessage.MessageType.TEXT,
            body=reply_text,
            delivery_status=WhatsAppMessage.DeliveryStatus.MANUAL_OPENED,
            estimated_cost_usd=Decimal("0.0000"),
        )
        feedback = "24h window closed or Cloud API disabled — tap 'Open Reply in WhatsApp' to send free."

    _enrich_contact_for_ui(contact, base_url)
    return render(
        request,
        "outreach/partials/contact_card.html",
        {"contact": contact, "feedback": feedback, "reply_wame_url": fallback_url},
    )


@require_POST
def add_note_view(request, pk: int):
    """HTMX endpoint: attach a pastoral / volunteer follow-up note."""
    contact = get_object_or_404(OutreachContact, pk=pk)
    note_text = (request.POST.get("note") or "").strip()
    author = (request.POST.get("author_name") or "Coordinator").strip()
    if note_text:
        FollowUpNote.objects.create(contact=contact, author_name=author[:120], note=note_text)

    base_url = request.build_absolute_uri("/").rstrip("/")
    _enrich_contact_for_ui(contact, base_url)
    return render(
        request,
        "outreach/partials/contact_card.html",
        {"contact": contact, "feedback": "Follow-up note saved."},
    )


@require_POST
def trigger_step_now_view(request, pk: int):
    """HTMX endpoint: run CostGuardRouter immediately for a contact."""
    contact = get_object_or_404(OutreachContact, pk=pk)
    result = CostGuardRouter().execute_drip_step(contact)
    base_url = request.build_absolute_uri("/").rstrip("/")
    _enrich_contact_for_ui(contact, base_url)
    return render(
        request,
        "outreach/partials/contact_card.html",
        {
            "contact": contact,
            "feedback": f"Router action: {result.get('action')} (cost: ${result.get('cost_usd', '0.0000')})",
        },
    )


@require_http_methods(["GET", "POST"])
def bulk_import_view(request):
    """Bulk CSV upload UI for up to 5,000+ outreach contacts with hourly stagger."""
    import_result = None
    if request.method == "POST":
        form = BulkImportForm(request.POST, request.FILES)
        if form.is_valid():
            cd = form.cleaned_data
            import_result = bulk_import_contacts_csv(
                file_obj=cd["csv_file"],
                max_per_hour=cd["max_per_hour"],
                default_location=cd.get("default_location", ""),
                default_soul_winner=cd.get("default_soul_winner", ""),
            )
    else:
        form = BulkImportForm()

    return render(
        request,
        "outreach/bulk_import.html",
        {
            "form": form,
            "import_result": import_result,
        },
    )


@require_GET
def sample_csv_view(request):
    """Provide a ready-to-fill sample CSV template for Ado-Ekiti outreach teams."""
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="carepulse_ado_ekiti_sample.csv"'
    writer = csv.writer(response)
    writer.writerow(["first_name", "last_name", "phone", "location_met", "prayer_point", "soul_winner_name"])
    writer.writerow(["Tunde", "Adebayo", "08031234567", "Fajuyi Park", "Career breakthrough", "Sis. Funke"])
    writer.writerow(["Bisi", "Olatunji", "+2348149876543", "EKSU Gate", "Final year exams", "Bro. Seun"])
    writer.writerow(["Chinedu", "Okafor", "09051122334", "Oja Oba", "Family peace & health", "Pastor Segun"])
    return response


@require_http_methods(["GET", "POST"])
def rsvp_card_view(request, rsvp_token: str):
    """Public 1-Tap Sunday RSVP & Prayer Update micro-page (Mode A + Mode B)."""
    contact = get_object_or_404(OutreachContact, rsvp_token=rsvp_token)

    if request.method == "POST":
        form = RsvpForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            contact.rsvp_status = cd["rsvp_status"]
            contact.rsvp_pickup_landmark = cd.get("rsvp_pickup_landmark", "").strip()
            if cd.get("prayer_point", "").strip():
                contact.prayer_point = cd["prayer_point"].strip()
            contact.rsvp_updated_at = timezone.now()
            contact.save(
                update_fields=[
                    "rsvp_status",
                    "rsvp_pickup_landmark",
                    "prayer_point",
                    "rsvp_updated_at",
                    "updated_at",
                ]
            )

            WhatsAppMessage.objects.create(
                contact=contact,
                direction=WhatsAppMessage.Direction.INBOUND,
                channel=WhatsAppMessage.Channel.SYSTEM,
                message_type=WhatsAppMessage.MessageType.TEXT,
                body=(
                    f"[Web RSVP] {contact.get_rsvp_status_display()}"
                    + (f" | Pickup: {contact.rsvp_pickup_landmark}" if contact.rsvp_pickup_landmark else "")
                    + (f" | Prayer: {contact.prayer_point}" if contact.prayer_point else "")
                ),
                delivery_status=WhatsAppMessage.DeliveryStatus.RECEIVED,
                estimated_cost_usd=Decimal("0.0000"),
            )
            return render(
                request,
                "outreach/rsvp_thanks.html",
                {
                    "contact": contact,
                    "church_wame_url": build_wa_me_url(
                        settings.CHURCH_WHATSAPP_NUMBER,
                        f"Hi {settings.CHURCH_SHORT_NAME}, I just submitted my Sunday RSVP ({contact.first_name}).",
                    ),
                },
            )
    else:
        form = RsvpForm(
            initial={
                "rsvp_status": (
                    contact.rsvp_status
                    if contact.rsvp_status != OutreachContact.RsvpStatus.NONE
                    else OutreachContact.RsvpStatus.ATTENDING
                ),
                "rsvp_pickup_landmark": contact.rsvp_pickup_landmark,
                "prayer_point": contact.prayer_point,
            }
        )

    return render(request, "outreach/rsvp_card.html", {"contact": contact, "form": form})


@csrf_exempt
@require_http_methods(["GET", "POST"])
def whatsapp_webhook_view(request):
    """
    Meta WhatsApp Cloud API Webhook endpoint:
      - GET: hub.challenge verification handshake
      - POST: HMAC-SHA256 verification + async Celery processing
    """
    if request.method == "GET":
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge", "")
        if mode == "subscribe" and token == settings.WA_WEBHOOK_VERIFY_TOKEN:
            return HttpResponse(challenge, content_type="text/plain", status=200)
        return HttpResponseForbidden("Webhook verification failed.")

    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_meta_webhook_signature(request.body, signature):
        return HttpResponseForbidden("Invalid webhook signature.")

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)

    try:
        process_webhook_event_task.delay(payload)
    except Exception as exc:
        if settings.DEBUG:
            logger.warning("Celery broker unavailable (%s); processing webhook inline in DEBUG mode.", exc)
            process_webhook_payload(payload)
        else:
            logger.exception("Failed to enqueue webhook payload")
            return JsonResponse({"error": "Queue unavailable"}, status=503)

    return JsonResponse({"status": "accepted"}, status=200)


@require_POST
def toggle_opt_out_view(request, pk: int):
    """HTMX endpoint: toggle STOP / opt-out state for a contact."""
    contact = get_object_or_404(OutreachContact, pk=pk)
    if contact.is_opted_out:
        contact.is_opted_out = False
        contact.drip_status = (
            OutreachContact.DripStatus.PENDING
            if settings.WA_CLOUD_API_ENABLED
            else OutreachContact.DripStatus.MANUAL_FALLBACK
        )
        feedback = "Contact re-enabled for outreach follow-up."
    else:
        contact.is_opted_out = True
        contact.drip_status = OutreachContact.DripStatus.OPTED_OUT
        contact.next_send_at = None
        feedback = "Contact opted out (STOP) — no further messages will be sent."

    contact.save(update_fields=["is_opted_out", "drip_status", "next_send_at", "updated_at"])
    base_url = request.build_absolute_uri("/").rstrip("/")
    _enrich_contact_for_ui(contact, base_url)
    return render(
        request,
        "outreach/partials/contact_card.html",
        {"contact": contact, "feedback": feedback},
    )


@require_GET
def export_rsvps_csv_view(request):
    """Export Sunday RSVP & Bus Pickup Landmark list as CSV for the Ado-Ekiti transport team."""
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="carepulse_sunday_rsvps.csv"'
    writer = csv.writer(response)
    writer.writerow(
        [
            "First Name",
            "Last Name",
            "Phone (E.164)",
            "Location Met",
            "RSVP Status",
            "Pickup Landmark",
            "Prayer Point",
            "Soul Winner",
            "Updated At",
        ]
    )
    qs = OutreachContact.objects.exclude(rsvp_status=OutreachContact.RsvpStatus.NONE).order_by(
        "rsvp_status", "rsvp_pickup_landmark", "first_name"
    )
    for c in qs:
        writer.writerow(
            [
                c.first_name,
                c.last_name,
                c.phone_e164,
                c.location_met,
                c.get_rsvp_status_display(),
                c.rsvp_pickup_landmark,
                c.prayer_point,
                c.soul_winner_name,
                c.rsvp_updated_at.isoformat() if c.rsvp_updated_at else "",
            ]
        )
    return response


@require_GET
def healthcheck_view(request):
    """Healthcheck endpoint for Docker/load-balancer readiness."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"status": "ok", "cloud_api_enabled": settings.WA_CLOUD_API_ENABLED})
