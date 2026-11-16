from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock

from django.test import TestCase, override_settings
from django.utils import timezone

from outreach.models import OutreachContact, WhatsAppMessage
from outreach.services import CostGuardRouter, MetaCloudAPIError
from outreach.tasks import get_remaining_tier_budget


class CostGuardRouterTests(TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client.send_utility_template.return_value = {
            "messaging_product": "whatsapp",
            "messages": [{"id": "wamid.template.001"}],
        }
        self.mock_client.send_interactive_buttons.return_value = {
            "messaging_product": "whatsapp",
            "messages": [{"id": "wamid.interactive.001"}],
        }
        self.router = CostGuardRouter(client=self.mock_client)

    @override_settings(WA_CLOUD_API_ENABLED=False)
    def test_mode_b_transitions_to_manual_fallback_at_zero_cost(self):
        contact = OutreachContact.objects.create(
            first_name="Tunde",
            phone_e164="+2348031234567",
            location_met="Fajuyi Park",
            drip_step=1,
            drip_status=OutreachContact.DripStatus.PENDING,
            next_send_at=timezone.now(),
        )
        result = self.router.execute_drip_step(contact)
        contact.refresh_from_db()

        self.assertEqual(result["action"], "manual_fallback")
        self.assertEqual(result["cost_usd"], Decimal("0.0000"))
        self.assertEqual(contact.drip_status, OutreachContact.DripStatus.MANUAL_FALLBACK)
        self.assertIsNone(contact.next_send_at)
        self.mock_client.send_utility_template.assert_not_called()
        self.mock_client.send_interactive_buttons.assert_not_called()

    @override_settings(WA_CLOUD_API_ENABLED=True, WA_STEP1_UTILITY_TEMPLATE="outreach_request_confirmation_v1")
    def test_step1_outside_window_sends_utility_template_only(self):
        now = timezone.now()
        contact = OutreachContact.objects.create(
            first_name="Bisi",
            phone_e164="+2348149876543",
            location_met="EKSU Gate",
            drip_step=1,
            drip_status=OutreachContact.DripStatus.PENDING,
            last_inbound_at=None,
        )
        result = self.router.execute_drip_step(contact, now=now)
        contact.refresh_from_db()

        self.assertEqual(result["action"], "sent_utility_template")
        self.assertEqual(result["cost_usd"], Decimal("0.0067"))
        self.assertEqual(contact.drip_status, OutreachContact.DripStatus.AWAITING_CONFIRMATION)
        self.assertEqual(contact.drip_step, 2)
        self.assertEqual(contact.next_send_at, now + timedelta(hours=24))
        self.mock_client.send_utility_template.assert_called_once()
        msg = WhatsAppMessage.objects.get(contact=contact)
        self.assertEqual(msg.message_type, WhatsAppMessage.MessageType.TEMPLATE)
        self.assertEqual(msg.estimated_cost_usd, Decimal("0.0067"))

    @override_settings(WA_CLOUD_API_ENABLED=True)
    def test_inside_24h_window_sends_free_interactive_and_chains_22h(self):
        now = timezone.now()
        contact = OutreachContact.objects.create(
            first_name="Chinedu",
            phone_e164="+2349051122334",
            location_met="Oja Oba",
            drip_step=1,
            drip_status=OutreachContact.DripStatus.ACTIVE,
            last_inbound_at=now - timedelta(minutes=10),
        )
        result = self.router.execute_drip_step(contact, now=now)
        contact.refresh_from_db()

        self.assertEqual(result["action"], "sent_interactive")
        self.assertEqual(result["cost_usd"], Decimal("0.0000"))
        self.assertEqual(contact.drip_step, 2)
        self.assertEqual(contact.drip_status, OutreachContact.DripStatus.ACTIVE)
        # 22-Hour Window Chaining!
        self.assertEqual(contact.next_send_at, now + timedelta(hours=22))
        self.mock_client.send_interactive_buttons.assert_called_once()
        self.mock_client.send_utility_template.assert_not_called()

    @override_settings(WA_CLOUD_API_ENABLED=True)
    def test_silent_contact_cutoff_on_step2_or_step3_outside_window(self):
        now = timezone.now()
        for step in (2, 3):
            contact = OutreachContact.objects.create(
                first_name=f"Silent{step}",
                phone_e164=f"+234803000000{step}",
                drip_step=step,
                drip_status=OutreachContact.DripStatus.AWAITING_CONFIRMATION,
                last_inbound_at=now - timedelta(hours=25),
            )
            result = self.router.execute_drip_step(contact, now=now)
            contact.refresh_from_db()

            self.assertEqual(result["action"], "manual_fallback")
            self.assertEqual(result["reason"], "silent_contact_cutoff")
            self.assertEqual(result["cost_usd"], Decimal("0.0000"))
            self.assertEqual(contact.drip_status, OutreachContact.DripStatus.MANUAL_FALLBACK)
            self.assertIsNone(contact.next_send_at)

        self.mock_client.send_utility_template.assert_not_called()
        self.mock_client.send_interactive_buttons.assert_not_called()

    @override_settings(WA_CLOUD_API_ENABLED=True)
    def test_meta_ecosystem_limit_131049_falls_back_to_manual(self):
        self.mock_client.send_utility_template.side_effect = MetaCloudAPIError(
            "Ecosystem engagement limit",
            error_code="131049",
            status_code=400,
        )
        contact = OutreachContact.objects.create(
            first_name="Seyi",
            phone_e164="+2348039998887",
            drip_step=1,
            drip_status=OutreachContact.DripStatus.PENDING,
        )
        result = self.router.execute_drip_step(contact)
        contact.refresh_from_db()

        self.assertEqual(result["action"], "fallback_on_error")
        self.assertEqual(contact.drip_status, OutreachContact.DripStatus.MANUAL_FALLBACK)
        self.assertEqual(result["cost_usd"], Decimal("0.0000"))

    @override_settings(WA_CLOUD_API_ENABLED=True, WA_MESSAGING_TIER_DAILY_LIMIT=250)
    def test_daily_tier_budget_counts_only_non_failed_templates(self):
        contact = OutreachContact.objects.create(
            first_name="Dayo",
            phone_e164="+2348021112233",
        )
        WhatsAppMessage.objects.create(
            contact=contact,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            channel=WhatsAppMessage.Channel.CLOUD_API,
            message_type=WhatsAppMessage.MessageType.TEMPLATE,
            delivery_status=WhatsAppMessage.DeliveryStatus.SENT,
            wamid="wamid.t1",
        )
        WhatsAppMessage.objects.create(
            contact=contact,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            channel=WhatsAppMessage.Channel.CLOUD_API,
            message_type=WhatsAppMessage.MessageType.INTERACTIVE,
            delivery_status=WhatsAppMessage.DeliveryStatus.SENT,
            wamid="wamid.i1",
        )
        self.assertEqual(get_remaining_tier_budget(), 249)
