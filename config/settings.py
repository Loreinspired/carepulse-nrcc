from decimal import Decimal
from pathlib import Path
import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, True),
    WA_CLOUD_API_ENABLED=(bool, False),
    WA_MESSAGING_TIER_DAILY_LIMIT=(int, 250),
    WA_BATCH_SIZE=(int, 50),
    WA_POLL_INTERVAL_SECONDS=(int, 30),
    WA_MAX_PER_HOUR=(int, 250),
)

env_file = BASE_DIR / ".env"
if env_file.exists():
    environ.Env.read_env(env_file)

SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    default="django-insecure-carepulse-ado-ekiti-local-dev-key-change-in-prod",
)
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "0.0.0.0", "*"])
SITE_BASE_URL = env("SITE_BASE_URL", default="http://localhost:8000").rstrip("/")
CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS",
    default=[SITE_BASE_URL] if SITE_BASE_URL.startswith(("http://", "https://")) else [],
)

# Auto-configure public hostname on free PaaS hosts (Render / Railway / Koyeb)
for _host_env in ("RENDER_EXTERNAL_HOSTNAME", "RAILWAY_PUBLIC_DOMAIN", "KOYEB_PUBLIC_DOMAIN"):
    _platform_host = env(_host_env, default="").strip()
    if _platform_host:
        if _platform_host not in ALLOWED_HOSTS:
            ALLOWED_HOSTS.append(_platform_host)
        _origin = f"https://{_platform_host}"
        if _origin not in CSRF_TRUSTED_ORIGINS:
            CSRF_TRUSTED_ORIGINS.append(_origin)
        if SITE_BASE_URL == "http://localhost:8000":
            SITE_BASE_URL = _origin

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_celery_beat",
    "outreach.apps.OutreachConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "outreach.context_processors.church_settings",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASE_URL = env("DATABASE_URL", default="")
if DATABASE_URL:
    DATABASES = {"default": env.db("DATABASE_URL")}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Lagos"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Celery Configuration
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/1")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

WA_POLL_INTERVAL_SECONDS = env("WA_POLL_INTERVAL_SECONDS")
CELERY_BEAT_SCHEDULE = {
    "carepulse-poll-due-drip-contacts": {
        "task": "outreach.tasks.poll_due_drip_contacts",
        "schedule": float(WA_POLL_INTERVAL_SECONDS),
    },
}

# Church & Outreach Settings
CHURCH_NAME = env("CHURCH_NAME", default="New Reality Christian Center")
CHURCH_SHORT_NAME = env("CHURCH_SHORT_NAME", default="New Reality Christian Center")
CHURCH_WHATSAPP_NUMBER = env("CHURCH_WHATSAPP_NUMBER", default="+2348000000000")
CHURCH_SUNDAY_TIME = env("CHURCH_SUNDAY_TIME", default="Sundays at 8:00 AM")
CHURCH_ADDRESS = env("CHURCH_ADDRESS", default="Ado-Ekiti, Ekiti State, Nigeria")

# Meta WhatsApp Cloud API & Cost-Guard Settings
WA_CLOUD_API_ENABLED = env("WA_CLOUD_API_ENABLED")
WA_API_VERSION = env("WA_API_VERSION", default="v22.0")
WA_PHONE_NUMBER_ID = env("WA_PHONE_NUMBER_ID", default="")
WA_WABA_ID = env("WA_WABA_ID", default="")
WA_ACCESS_TOKEN = env("WA_ACCESS_TOKEN", default="")
WA_APP_SECRET = env("WA_APP_SECRET", default="")
WA_WEBHOOK_VERIFY_TOKEN = env("WA_WEBHOOK_VERIFY_TOKEN", default="carepulse-ado-ekiti-verify-token")

WA_STEP1_UTILITY_TEMPLATE = env(
    "WA_STEP1_UTILITY_TEMPLATE",
    default="outreach_request_confirmation_v1",
)
WA_TEMPLATE_LANGUAGE = env("WA_TEMPLATE_LANGUAGE", default="en")
WA_UTILITY_TEMPLATE_COST_USD = Decimal(
    env("WA_UTILITY_TEMPLATE_COST_USD", default="0.0067")
)

WA_MESSAGING_TIER_DAILY_LIMIT = env("WA_MESSAGING_TIER_DAILY_LIMIT")
WA_BATCH_SIZE = env("WA_BATCH_SIZE")
WA_MAX_PER_HOUR = env("WA_MAX_PER_HOUR")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}
