import os
import subprocess
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from outreach.models import OutreachContact

BASE_DIR = Path(settings.BASE_DIR)


class LoginRequiredTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.contact = OutreachContact.objects.create(
            first_name="Private",
            phone_e164="+2348031234569",
            rsvp_status=OutreachContact.RsvpStatus.ATTENDING,
        )

    def test_coordinator_pages_redirect_anonymous_users_to_login(self):
        protected_gets = [
            reverse("outreach:field_capture"),
            reverse("outreach:dashboard"),
            reverse("outreach:export_rsvps_csv"),
            reverse("outreach:bulk_import"),
            reverse("outreach:sample_csv"),
            reverse("outreach:field_qr_handshake", args=[self.contact.rsvp_token]),
        ]
        for url in protected_gets:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, url)
            self.assertTrue(resp["Location"].startswith(reverse("login")), url)

        protected_posts = [
            reverse("outreach:mark_manual_sent", args=[self.contact.pk]),
            reverse("outreach:coordinator_reply", args=[self.contact.pk]),
            reverse("outreach:add_note", args=[self.contact.pk]),
            reverse("outreach:trigger_step_now", args=[self.contact.pk]),
            reverse("outreach:toggle_opt_out", args=[self.contact.pk]),
        ]
        for url in protected_posts:
            resp = self.client.post(url, {"step": 1, "note": "x", "reply_text": "x"})
            self.assertEqual(resp.status_code, 302, url)

        self.contact.refresh_from_db()
        self.assertFalse(self.contact.is_opted_out)
        self.assertEqual(self.contact.notes.count(), 0)

    def test_export_does_not_leak_data_to_anonymous_users(self):
        resp = self.client.get(reverse("outreach:export_rsvps_csv"))
        self.assertNotIn(b"2348031234569", resp.content)

    def test_htmx_request_gets_full_page_redirect_instead_of_swapping_login_form(self):
        resp = self.client.post(
            reverse("outreach:toggle_opt_out", args=[self.contact.pk]),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp["HX-Redirect"], reverse("login"))

    def test_public_endpoints_stay_public(self):
        self.assertEqual(self.client.get(reverse("outreach:rsvp_card", args=[self.contact.rsvp_token])).status_code, 200)
        self.assertEqual(self.client.get(reverse("outreach:healthcheck")).status_code, 200)
        self.assertEqual(self.client.get(reverse("login")).status_code, 200)

    def test_handshake_is_not_reachable_by_sequential_id(self):
        user = get_user_model().objects.create_user(username="vol", password="pw-for-tests-123")
        self.client.force_login(user)
        self.assertEqual(self.client.get(f"/field/handshake/{self.contact.pk}/").status_code, 404)
        self.assertEqual(
            self.client.get(reverse("outreach:field_qr_handshake", args=[self.contact.rsvp_token])).status_code,
            200,
        )

    def test_login_flow_and_logout(self):
        get_user_model().objects.create_user(username="sis.funke", password="pw-for-tests-123")
        resp = self.client.post(
            reverse("login"),
            {"username": "sis.funke", "password": "pw-for-tests-123", "next": reverse("outreach:dashboard")},
        )
        self.assertRedirects(resp, reverse("outreach:dashboard"))
        dash = self.client.get(reverse("outreach:dashboard"))
        self.assertContains(dash, "Log out (sis.funke)")
        self.assertNotContains(dash, 'href="/admin/"')  # non-staff volunteers don't see Admin

        self.client.post(reverse("logout"))
        self.assertEqual(self.client.get(reverse("outreach:dashboard")).status_code, 302)


@override_settings(ALLOWED_HOSTS=["carepulse.example.org"])
class HealthCheckMiddlewareTests(TestCase):
    def test_healthcheck_answers_platform_probe_with_internal_host_header(self):
        resp = Client().get("/healthz/", HTTP_HOST="10.0.0.5:10000")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")

    def test_other_paths_still_validate_host(self):
        resp = Client().get("/r/whatever/", HTTP_HOST="10.0.0.5:10000")
        self.assertEqual(resp.status_code, 400)


class WebhookFailsClosedTests(TestCase):
    url = "/webhooks/whatsapp/"

    @override_settings(WA_APP_SECRET="", DEBUG=False)
    @patch("outreach.views.process_webhook_event_task.delay")
    def test_unsigned_webhook_rejected_in_production_without_app_secret(self, mock_delay):
        resp = Client().post(self.url, data=b'{"entry": []}', content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        mock_delay.assert_not_called()

    @override_settings(WA_APP_SECRET="", DEBUG=True)
    @patch("outreach.views.process_webhook_event_task.delay")
    def test_unsigned_webhook_allowed_only_for_local_debug(self, mock_delay):
        resp = Client().post(self.url, data=b'{"entry": []}', content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        mock_delay.assert_called_once()

    @override_settings(WA_WEBHOOK_VERIFY_TOKEN="")
    def test_empty_verify_token_never_matches(self):
        resp = Client().get(self.url, {"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "42"})
        self.assertEqual(resp.status_code, 403)


class EnsureSuperuserCommandTests(TestCase):
    def test_creates_admin_once_and_never_modifies_existing(self):
        env = {"DJANGO_SUPERUSER_USERNAME": "pastor", "DJANGO_SUPERUSER_PASSWORD": "first-pass-123"}
        with patch.dict(os.environ, env):
            call_command("ensure_superuser", stdout=StringIO())
        user = get_user_model().objects.get(username="pastor")
        self.assertTrue(user.is_superuser and user.is_staff)
        self.assertTrue(user.check_password("first-pass-123"))

        with patch.dict(os.environ, {**env, "DJANGO_SUPERUSER_PASSWORD": "changed-pass-456"}):
            call_command("ensure_superuser", stdout=StringIO())
        user.refresh_from_db()
        self.assertTrue(user.check_password("first-pass-123"))
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_skips_quietly_when_not_configured(self):
        with patch.dict(os.environ, {"DJANGO_SUPERUSER_USERNAME": "", "DJANGO_SUPERUSER_PASSWORD": ""}):
            call_command("ensure_superuser", stdout=StringIO())
        self.assertEqual(get_user_model().objects.count(), 0)


class ProductionSettingsTests(TestCase):
    """Load settings in a subprocess, the way a real deploy would (not under `manage.py test`)."""

    def _check(self, **env_overrides) -> subprocess.CompletedProcess:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("DJANGO_", "WA_", "RENDER_", "RAILWAY_", "KOYEB_"))
        }
        env.update({"DJANGO_DEBUG": "False", "DJANGO_SECRET_KEY": "", "WA_CLOUD_API_ENABLED": "False"})
        env.update(env_overrides)
        return subprocess.run(
            [sys.executable, "manage.py", "check"],
            cwd=BASE_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_refuses_to_start_without_real_secret_key(self):
        for bad in ("", "change-me-to-a-long-random-production-secret-key"):
            result = self._check(DJANGO_SECRET_KEY=bad)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("DJANGO_SECRET_KEY must be set", result.stderr)

    def test_cloud_api_requires_app_secret_in_production(self):
        result = self._check(
            DJANGO_SECRET_KEY="x" * 60,
            WA_CLOUD_API_ENABLED="True",
            WA_PHONE_NUMBER_ID="123",
            WA_ACCESS_TOKEN="token",
            WA_WEBHOOK_VERIFY_TOKEN="verify",
            WA_APP_SECRET="",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("WA_APP_SECRET", result.stderr)

    def test_valid_production_config_passes_deploy_checks(self):
        result = self._check(DJANGO_SECRET_KEY="k" * 20 + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789")
        self.assertEqual(result.returncode, 0, result.stderr)
