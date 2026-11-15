from django.conf import settings


def church_settings(request):
    """Inject church branding and WhatsApp mode flags into all templates."""
    return {
        "CHURCH_NAME": settings.CHURCH_NAME,
        "CHURCH_SHORT_NAME": settings.CHURCH_SHORT_NAME,
        "CHURCH_WHATSAPP_NUMBER": settings.CHURCH_WHATSAPP_NUMBER,
        "CHURCH_SUNDAY_TIME": settings.CHURCH_SUNDAY_TIME,
        "CHURCH_ADDRESS": settings.CHURCH_ADDRESS,
        "WA_CLOUD_API_ENABLED": settings.WA_CLOUD_API_ENABLED,
    }
