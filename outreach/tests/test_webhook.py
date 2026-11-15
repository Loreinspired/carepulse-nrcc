import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from outreach.models import OutreachContact, WhatsAppMessage
from outreach.services import process_webhook_payload


class WebhookAndRsvpTests(TestCase):
    def setUp(self):
        self.client = Client()

    @override_settings(WA_WEBHOOK_VERIFY_TOKEN="test-verify-token")
    def test_webhook_get_verification(self):
        url = reverse("outreach:whatsapp_webhook")
        resp_ok = self.client.get(
            url,
            {
                "hub.mode": "subscribe",
                "hub.verify_token": "test-verify-token",
                "hub.challenge": "123456789",
            },
        )
        self.assertEqual(resp_ok.status_code, 200)
        self.assertEqual(resp_ok.content.decode(), "123456789")

        resp_bad = self.client.get(
            url,
            {
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong-token",
                "hub.challenge": "123456789",
            },
        )
        self.assertEqual(resp_bad.status_code, 403)

    @override_settings(WA_APP_SECRET="super-secret-app-key")
    @patch("outreach.views.process_webhook_event_task.delay")
    def test_webhook_post_signature_verification_and_dispatch(self, mock_delay):
        url = reverse("outreach:whatsapp_webhook")
        payload = {"object": "whatsapp_business_account", "entry": []}
        body = json.dumps(payload).encode("utf-8")
        sig = "sha256=" + hmac.new(b"super-secret-app-key", body, hashlib.sha256).hexdigest()

        resp_ok = self.client.post(
            url,
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=sig,
        )
        self.assertEqual(resp_ok.status_code, 200)
        mock_delay.assert_called_once_with(payload)

        resp_bad = self.client.post(
            url,
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256="sha256=invalid",
        )
        self.assertEqual(resp_bad.status_code, 403)

    @override_settings(WA_CLOUD_API_ENABLED=True)
    def test_quick_reply_confirmation_opens_window_and_chains_step2_in_22h(self):
        contact = OutreachContact.objects.create(
            first_name="Kemi",
            phone_e164="+2348035556677",
            wa_id="2348035556677",
            location_met="Fajuyi Park",
            drip_step=2,
            drip_status=OutreachContact.DripStatus.AWAITING_CONFIRMATION,
        )
        mock_client = MagicMock()
        mock_client.send_interactive_buttons.return_value = {
            "messages": [{"id": "wamid.welcome.001"}]
        }

        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "contacts": [{"wa_id": "2348035556677", "profile": {"name": "Kemi"}}],
                                "messages": [
                                    {
                                        "from": "2348035556677",
                                        "id": "wamid.inbound.btn.1",
                                        "type": "button",
                                        "button": {"payload": "Confirm & Connect", "text": "Confirm & Connect"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }

        stats = process_webhook_payload(payload, client=mock_client)
        self.assertEqual(stats["messages_processed"], 1)

        # Duplicate delivery must be ignored
        dup_stats = process_webhook_payload(payload, client=mock_client)
        self.assertEqual(dup_stats["duplicates_skipped"], 1)

        contact.refresh_from_db()
        self.assertIsNotNone(contact.last_inbound_at)
        self.assertEqual(contact.drip_status, OutreachContact.DripStatus.ACTIVE)
        self.assertEqual(contact.drip_step, 2)
        self.assertIsNotNone(contact.next_send_at)
        # 22h window chaining from last_inbound_at
        delta_hours = (contact.next_send_at - contact.last_inbound_at).total_seconds() / 3600.0
        self.assertAlmostEqual(delta_hours, 22.0, delta=0.05)

        mock_client.send_interactive_buttons.assert_called_once()
        outbound = WhatsAppMessage.objects.get(wamid="wamid.welcome.001")
        self.assertEqual(outbound.estimated_cost_usd, Decimal("0.0000"))

    def test_inbound_stop_opts_out_contact(self):
        contact = OutreachContact.objects.create(
            first_name="Kunle",
            phone_e164="+2348067778899",
            wa_id="2348067778899",
            drip_status=OutreachContact.DripStatus.ACTIVE,
        )
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "2348067778899",
                                        "id": "wamid.stop.1",
                                        "type": "text",
                                        "text": {"body": "STOP"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        process_webhook_payload(payload, client=MagicMock())
        contact.refresh_from_db()
        self.assertTrue(contact.is_opted_out)
        self.assertEqual(contact.drip_status, OutreachContact.DripStatus.OPTED_OUT)
        self.assertIsNone(contact.next_send_at)

    def test_inbound_voice_note_downloads_audio_file(self):
        contact = OutreachContact.objects.create(
            first_name="Yemi",
            phone_e164="+2348123334455",
            wa_id="2348123334455",
            drip_status=OutreachContact.DripStatus.ACTIVE,
        )
        mock_client = MagicMock()
        mock_client.download_media.return_value = (b"OggS-fake-opus-bytes", "audio/ogg; codecs=opus")

        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "2348123334455",
                                        "id": "wamid.audio.1",
                                        "type": "audio",
                                        "audio": {"id": "media_998877", "mime_type": "audio/ogg; codecs=opus"},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        process_webhook_payload(payload, client=mock_client)

        msg = WhatsAppMessage.objects.get(wamid="wamid.audio.1")
        self.assertEqual(msg.message_type, WhatsAppMessage.MessageType.AUDIO)
        self.assertTrue(bool(msg.media_file.name))
        self.assertTrue(msg.media_file.name.endswith(".ogg"))
        msg.media_file.delete(save=False)

    def test_public_rsvp_card_syncs_attendance_and_prayer(self):
        contact = OutreachContact.objects.create(
            first_name="Ayo",
            phone_e164="+2347034445566",
            location_met="EKSU Gate",
        )
        url = reverse("outreach:rsvp_card", args=[contact.rsvp_token])
        resp = self.client.post(
            url,
            {
                "rsvp_status": OutreachContact.RsvpStatus.NEED_RIDE,
                "rsvp_pickup_landmark": "EKSU Main Gate",
                "prayer_point": "Final year project defense",
            },
        )
        self.assertEqual(resp.status_code, 200)
        contact.refresh_from_db()
        self.assertEqual(contact.rsvp_status, OutreachContact.RsvpStatus.NEED_RIDE)
        self.assertEqual(contact.rsvp_pickup_landmark, "EKSU Main Gate")
        self.assertEqual(contact.prayer_point, "Final year project defense")
        self.assertTrue(
            WhatsAppMessage.objects.filter(
                contact=contact,
                channel=WhatsAppMessage.Channel.SYSTEM,
            ).exists()
        )
