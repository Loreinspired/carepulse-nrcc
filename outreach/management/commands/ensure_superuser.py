import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Create the first admin account from DJANGO_SUPERUSER_USERNAME / DJANGO_SUPERUSER_PASSWORD "
        "(and optional DJANGO_SUPERUSER_EMAIL) if it does not exist yet. Safe to run on every deploy: "
        "an existing account is never modified. Useful on hosts without a shell (e.g. Render free tier)."
    )

    def handle(self, *args, **options):
        username = os.environ.get("DJANGO_SUPERUSER_USERNAME", "").strip()
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD", "")
        email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "").strip()

        if not username or not password:
            self.stdout.write("ensure_superuser: DJANGO_SUPERUSER_USERNAME/PASSWORD not set; skipping.")
            return

        User = get_user_model()
        if User.objects.filter(username=username).exists():
            self.stdout.write(f"ensure_superuser: user '{username}' already exists; leaving it unchanged.")
            return

        User.objects.create_superuser(username=username, email=email, password=password)
        self.stdout.write(self.style.SUCCESS(f"ensure_superuser: created admin user '{username}'."))
