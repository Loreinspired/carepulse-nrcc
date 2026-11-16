from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from outreach.models import OutreachContact, WhatsAppMessage
from outreach.tasks import poll_due_drip_contacts


class ViewsAndPollerTests(TestCase):
    def setUp(self):
        self.client = Client()

    @override_settings(WA_CLOUD_API_ENABLED=False)
    def test_field_capture_and_qr_handshake(self):
        resp = self.client.post(
            reverse("outreach:field_capture"),
            {
                "first_name": "Tunde",
                "last_name": "Adebayo",
                "phone": "0803 123 4567",
                "location_met": "Fajuyi Park",
                "prayer_point": "Job breakthrough",
                "soul_winner_name": "Sis. Funke",
            },
        )
        self.assertEqual(resp.status_code, 302)
        contact = OutreachContact.objects.get(phone_e164="+2348031234567")
        self.assertEqual(contact.drip_status, OutreachContact.DripStatus.MANUAL_FALLBACK)

        handshake_resp = self.client.get(resp["Location"])
        self.assertEqual(handshake_resp.status_code, 200)
        self.assertContains(handshake_resp, "<svg")
        self.assertContains(handshake_resp, "https://wa.me/")
        self.assertContains(handshake_resp, "I was invited to church around Fajuyi Park")

        # Switching to GOT_SAVED updates the pre-filled handshake message
        saved_resp = self.client.get(resp["Location"] + "?outcome=GOT_SAVED")
        self.assertEqual(saved_resp.status_code, 200)
        self.assertContains(saved_resp, "I just got saved around Fajuyi Park")
        contact.refresh_from_db()
        self.assertEqual(contact.outreach_outcome, OutreachContact.OutreachOutcome.GOT_SAVED)

    def test_coordinator_dashboard_and_manual_wame_step_progression(self):
        contact = OutreachContact.objects.create(
            first_name="Segun",
            phone_e164="+2348145556677",
            location_met="Oja Oba",
            drip_step=1,
            drip_status=OutreachContact.DripStatus.MANUAL_FALLBACK,
        )

        dash_resp = self.client.get(reverse("outreach:dashboard"))
        self.assertEqual(dash_resp.status_code, 200)
        self.assertContains(dash_resp, "Segun")
        self.assertContains(dash_resp, "1-Tap Open in WhatsApp")

        # Mark Steps 1, 2, 3 via HTMX
        mark_url = reverse("outreach:mark_manual_sent", args=[contact.pk])
        for expected_step in (2, 3):
            r = self.client.post(mark_url, HTTP_HX_REQUEST="true")
            self.assertEqual(r.status_code, 200)
            contact.refresh_from_db()
            self.assertEqual(contact.drip_step, expected_step)
            self.assertEqual(contact.drip_status, OutreachContact.DripStatus.MANUAL_FALLBACK)

        r3 = self.client.post(mark_url, HTTP_HX_REQUEST="true")
        self.assertEqual(r3.status_code, 200)
        contact.refresh_from_db()
        self.assertEqual(contact.drip_status, OutreachContact.DripStatus.COMPLETED)
        self.assertEqual(
            WhatsAppMessage.objects.filter(
                contact=contact,
                channel=WhatsAppMessage.Channel.MANUAL_WAME,
                estimated_cost_usd=Decimal("0.0000"),
            ).count(),
            3,
        )

    def test_export_rsvps_csv_and_healthcheck(self):
        OutreachContact.objects.create(
            first_name="Folake",
            phone_e164="+2349012223344",
            location_met="EKSU Gate",
            rsvp_status=OutreachContact.RsvpStatus.NEED_RIDE,
            rsvp_pickup_landmark="EKSU Main Gate",
        )
        csv_resp = self.client.get(reverse("outreach:export_rsvps_csv"))
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn("Folake", csv_resp.content.decode())
        self.assertIn("EKSU Main Gate", csv_resp.content.decode())

        health_resp = self.client.get(reverse("outreach:healthcheck"))
        self.assertEqual(health_resp.status_code, 200)
        self.assertEqual(health_resp.json()["status"], "ok")

    @patch("outreach.tasks.execute_drip_step_task.delay")
    def test_poll_due_drip_contacts_claims_and_leases(self, mock_delay):
        now = timezone.now()
        due = OutreachContact.objects.create(
            first_name="DueSoul",
            phone_e164="+2348031110001",
            drip_status=OutreachContact.DripStatus.PENDING,
            next_send_at=now - timedelta(minutes=1),
        )
        OutreachContact.objects.create(
            first_name="FutureSoul",
            phone_e164="+2348031110002",
            drip_status=OutreachContact.DripStatus.PENDING,
            next_send_at=now + timedelta(hours=2),
        )

        with self.captureOnCommitCallbacks(execute=True):
            res = poll_due_drip_contacts()

        self.assertEqual(res["claimed"], 1)
        mock_delay.assert_called_once_with(due.pk)
        due.refresh_from_db()
        self.assertGreater(due.next_send_at, now)
