from django.conf import settings


def _queue_statuses():
    from outreach.models import OutreachContact

    statuses = [OutreachContact.DripStatus.MANUAL_FALLBACK]
    if not settings.WA_CLOUD_API_ENABLED:
        statuses.append(OutreachContact.DripStatus.PENDING)
    return statuses


def church_settings(request):
    """Inject church branding, WhatsApp mode flags and nav badges into all templates."""

    def nav_queue_count():
        # Callable: Django only runs the query if a template actually uses the value.
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return 0
        from outreach.models import OutreachContact

        return OutreachContact.objects.filter(is_opted_out=False, drip_status__in=_queue_statuses()).count()

    return {
        "CHURCH_NAME": settings.CHURCH_NAME,
        "CHURCH_SHORT_NAME": settings.CHURCH_SHORT_NAME,
        "CHURCH_WHATSAPP_NUMBER": settings.CHURCH_WHATSAPP_NUMBER,
        "CHURCH_SUNDAY_TIME": settings.CHURCH_SUNDAY_TIME,
        "CHURCH_ADDRESS": settings.CHURCH_ADDRESS,
        "WA_CLOUD_API_ENABLED": settings.WA_CLOUD_API_ENABLED,
        "nav_queue_count": nav_queue_count,
    }
