from django.contrib import admin

from outreach.models import FollowUpNote, OutreachContact, WhatsAppMessage


class WhatsAppMessageInline(admin.TabularInline):
    model = WhatsAppMessage
    extra = 0
    readonly_fields = (
        "direction",
        "channel",
        "message_type",
        "drip_step",
        "wamid",
        "delivery_status",
        "estimated_cost_usd",
        "created_at",
    )


class FollowUpNoteInline(admin.TabularInline):
    model = FollowUpNote
    extra = 0


@admin.register(OutreachContact)
class OutreachContactAdmin(admin.ModelAdmin):
    list_display = (
        "first_name",
        "last_name",
        "phone_e164",
        "location_met",
        "drip_step",
        "drip_status",
        "rsvp_status",
        "last_inbound_at",
        "next_send_at",
    )
    list_filter = ("drip_status", "drip_step", "rsvp_status", "capture_source", "is_opted_out")
    search_fields = ("first_name", "last_name", "phone_e164", "location_met")
    readonly_fields = ("rsvp_token", "created_at", "updated_at")
    inlines = [WhatsAppMessageInline, FollowUpNoteInline]


@admin.register(WhatsAppMessage)
class WhatsAppMessageAdmin(admin.ModelAdmin):
    list_display = (
        "contact",
        "direction",
        "channel",
        "message_type",
        "delivery_status",
        "estimated_cost_usd",
        "created_at",
    )
    list_filter = ("direction", "channel", "message_type", "delivery_status")
    search_fields = ("contact__first_name", "contact__phone_e164", "wamid", "body")
    readonly_fields = ("created_at",)


@admin.register(FollowUpNote)
class FollowUpNoteAdmin(admin.ModelAdmin):
    list_display = ("contact", "author_name", "created_at")
    search_fields = ("contact__first_name", "author_name", "note")
