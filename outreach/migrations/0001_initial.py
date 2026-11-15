from decimal import Decimal
import django.db.models.deletion
from django.db import migrations, models

import outreach.models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="OutreachContact",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("first_name", models.CharField(max_length=100)),
                ("last_name", models.CharField(blank=True, max_length=100)),
                ("phone_raw", models.CharField(blank=True, max_length=32)),
                ("phone_e164", models.CharField(db_index=True, max_length=20, unique=True)),
                ("wa_id", models.CharField(blank=True, db_index=True, max_length=20)),
                (
                    "location_met",
                    models.CharField(
                        blank=True,
                        help_text="E.g., Fajuyi Park, Oja Oba, EKSU Gate, ABUAD Axis",
                        max_length=150,
                    ),
                ),
                ("prayer_point", models.TextField(blank=True)),
                ("soul_winner_name", models.CharField(blank=True, max_length=120)),
                (
                    "capture_source",
                    models.CharField(
                        choices=[
                            ("FIELD_FORM", "Field Soul-Winner Form"),
                            ("CSV_IMPORT", "Bulk CSV Import"),
                            ("INBOUND_QR", "Inbound WhatsApp / QR"),
                            ("ADMIN", "Admin Entry"),
                        ],
                        default="FIELD_FORM",
                        max_length=24,
                    ),
                ),
                ("drip_step", models.PositiveSmallIntegerField(default=1)),
                (
                    "drip_status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending Drip"),
                            ("AWAITING_CONFIRMATION", "Awaiting Confirmation"),
                            ("ACTIVE", "Active (24h Free Window)"),
                            ("REPLIED", "Replied / Engaged"),
                            ("MANUAL_FALLBACK", "Manual wa.me Queue"),
                            ("COMPLETED", "Drip Completed"),
                            ("OPTED_OUT", "Opted Out (STOP)"),
                            ("INVALID_NUMBER", "Invalid Number"),
                        ],
                        db_index=True,
                        default="PENDING",
                        max_length=32,
                    ),
                ),
                ("next_send_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("last_inbound_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("last_outbound_at", models.DateTimeField(blank=True, null=True)),
                ("is_opted_out", models.BooleanField(db_index=True, default=False)),
                ("manual_sent_step", models.PositiveSmallIntegerField(default=0)),
                ("assigned_volunteer", models.CharField(blank=True, max_length=120)),
                (
                    "rsvp_token",
                    models.CharField(
                        db_index=True,
                        default=outreach.models.generate_rsvp_token,
                        max_length=32,
                        unique=True,
                    ),
                ),
                (
                    "rsvp_status",
                    models.CharField(
                        choices=[
                            ("NONE", "No RSVP Yet"),
                            ("ATTENDING", "Yes, Coming This Sunday"),
                            ("NEED_RIDE", "Coming — Need Free Ride"),
                            ("MAYBE", "Might Come"),
                            ("PRAYER_ONLY", "Pray With Me (Can't Attend)"),
                        ],
                        db_index=True,
                        default="NONE",
                        max_length=24,
                    ),
                ),
                ("rsvp_pickup_landmark", models.CharField(blank=True, max_length=150)),
                ("rsvp_updated_at", models.DateTimeField(blank=True, null=True)),
                ("import_batch_id", models.CharField(blank=True, db_index=True, max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["drip_status", "next_send_at"], name="idx_drip_poll_due"),
                    models.Index(fields=["drip_status", "drip_step"], name="idx_drip_status_step"),
                ],
            },
        ),
        migrations.CreateModel(
            name="WhatsAppMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "direction",
                    models.CharField(
                        choices=[("INBOUND", "Inbound"), ("OUTBOUND", "Outbound")],
                        db_index=True,
                        max_length=16,
                    ),
                ),
                (
                    "channel",
                    models.CharField(
                        choices=[
                            ("CLOUD_API", "Meta Cloud API"),
                            ("MANUAL_WAME", "Manual 1-Tap wa.me"),
                            ("SYSTEM", "System / Web RSVP"),
                        ],
                        default="CLOUD_API",
                        max_length=24,
                    ),
                ),
                (
                    "message_type",
                    models.CharField(
                        choices=[
                            ("TEXT", "Text"),
                            ("INTERACTIVE", "Interactive Buttons"),
                            ("TEMPLATE", "Utility Template"),
                            ("AUDIO", "Voice Note / Audio"),
                            ("IMAGE", "Image"),
                            ("DOCUMENT", "Document"),
                            ("BUTTON", "Quick Reply Tap"),
                            ("UNKNOWN", "Other"),
                        ],
                        default="TEXT",
                        max_length=24,
                    ),
                ),
                ("drip_step", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("wamid", models.CharField(blank=True, db_index=True, max_length=160, null=True, unique=True)),
                ("body", models.TextField(blank=True)),
                ("template_name", models.CharField(blank=True, max_length=128)),
                ("payload_json", models.JSONField(blank=True, default=dict)),
                (
                    "delivery_status",
                    models.CharField(
                        choices=[
                            ("QUEUED", "Queued"),
                            ("SENT", "Sent"),
                            ("DELIVERED", "Delivered"),
                            ("READ", "Read"),
                            ("FAILED", "Failed"),
                            ("MANUAL_OPENED", "Opened in WhatsApp"),
                            ("RECEIVED", "Received"),
                        ],
                        db_index=True,
                        default="QUEUED",
                        max_length=24,
                    ),
                ),
                ("error_code", models.CharField(blank=True, max_length=32)),
                ("error_message", models.TextField(blank=True)),
                ("estimated_cost_usd", models.DecimalField(decimal_places=4, default=Decimal("0.0000"), max_digits=8)),
                ("media_id", models.CharField(blank=True, max_length=160)),
                ("media_mime_type", models.CharField(blank=True, max_length=100)),
                ("media_file", models.FileField(blank=True, null=True, upload_to="whatsapp_media/%Y/%m/")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "contact",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="messages",
                        to="outreach.outreachcontact",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at"],
            },
        ),
        migrations.CreateModel(
            name="FollowUpNote",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("author_name", models.CharField(default="Follow-Up Team", max_length=120)),
                ("note", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "contact",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notes",
                        to="outreach.outreachcontact",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
    ]
