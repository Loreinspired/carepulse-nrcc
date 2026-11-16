from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from outreach.models import OutreachContact, WhatsAppMessage
from outreach.services import (
    SEND_LEASE,
    CostGuardRouter,
    MetaCloudAPIError,
    MetaWhatsAppClient,
    _get_or_create_inbound_contact,
    _handle_inbound_message,
    process_webhook_payload,
)
from outreach.tasks import execute_drip_step_task, poll_due_drip_contacts


def inbound_payload(wa_id: str, wamid: str, text: str = "Hi", name: str = "Tunde") -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": wa_id, "profile": {"name": name}}],
                            "messages": [{"from": wa_id, "id": wamid, "type": "text", "text": {"body": text}}],
                        }
                    }
                ]
            }
        ]
    }


def mock_meta_client() -> MagicMock:
    client = MagicMock()
    counter = {"n": 0}

    def _resp(*args, **kwargs):
        counter["n"] += 1
        return {"messages": [{"id": f"wamid.out.{counter['n']}"}]}

    client.send_interactive_buttons.side_effect = _resp
    client.send_utility_template.side_effect = _resp
    client.send_text.side_effect = _resp
    return client


@override_settings(WA_CLOUD_API_ENABLED=True)
class DripStateMachineTests(TestCase):
    def setUp(self):
        self.client_mock = mock_meta_client()
        self.router = CostGuardRouter(client=self.client_mock)

    def test_qr_handshake_after_field_capture_sends_free_welcome_immediately(self):
        contact = OutreachContact.objects.create(
            first_name="Tunde",
            phone_e164="+2348031234567",
            drip_step=1,
            drip_status=OutreachContact.DripStatus.PENDING,
            next_send_at=timezone.now() + timedelta(minutes=2),
        )
        process_webhook_payload(inbound_payload("2348031234567", "wamid.in.qr"), client=self.client_mock)
        contact.refresh_from_db()

        self.client_mock.send_interactive_buttons.assert_called_once()
        self.client_mock.send_utility_template.assert_not_called()
        self.assertEqual(contact.drip_status, OutreachContact.DripStatus.ACTIVE)
        self.assertEqual(contact.drip_step, 2)
        hours = (contact.next_send_at - contact.last_inbound_at).total_seconds() / 3600
        self.assertAlmostEqual(hours, 22.0, delta=0.05)
        welcome = WhatsAppMessage.objects.get(contact=contact, direction=WhatsAppMessage.Direction.OUTBOUND)
        self.assertEqual(welcome.delivery_status, WhatsAppMessage.DeliveryStatus.SENT)
        self.assertEqual(welcome.estimated_cost_usd, Decimal("0.0000"))

    def test_brand_new_inbound_number_gets_welcome(self):
        process_webhook_payload(
            inbound_payload("2348099990000", "wamid.in.new", name="Bisi Ade"), client=self.client_mock
        )
        contact = OutreachContact.objects.get(phone_e164="+2348099990000")
        self.assertEqual(contact.first_name, "Bisi")
        self.assertEqual(contact.capture_source, OutreachContact.CaptureSource.INBOUND_QR)
        self.client_mock.send_interactive_buttons.assert_called_once()
        self.assertEqual(contact.drip_step, 2)

    def test_reply_mid_drip_keeps_drip_running_and_shows_in_inbox(self):
        now = timezone.now()
        contact = OutreachContact.objects.create(
            first_name="Kemi",
            phone_e164="+2348035556677",
            wa_id="2348035556677",
            drip_step=3,
            drip_status=OutreachContact.DripStatus.ACTIVE,
            last_inbound_at=now - timedelta(hours=20),
            next_send_at=now + timedelta(hours=30),
        )
        process_webhook_payload(inbound_payload("2348035556677", "wamid.in.mid", "Thank you!"), client=self.client_mock)
        contact.refresh_from_db()

        self.assertEqual(contact.drip_status, OutreachContact.DripStatus.ACTIVE)
        self.assertEqual(contact.drip_step, 3)
        # Pulled in so Step 3 lands inside the window this reply opened
        self.assertLessEqual(contact.next_send_at, contact.last_inbound_at + timedelta(hours=22))
        self.client_mock.send_interactive_buttons.assert_not_called()

        user = get_user_model().objects.create_user(username="coord", password="pw-for-tests-123")
        web = Client()
        web.force_login(user)
        self.assertContains(web.get(reverse("outreach:dashboard") + "?tab=inbox"), "Kemi")

    def test_reply_from_contact_outside_drip_goes_to_replied_inbox(self):
        contact = OutreachContact.objects.create(
            first_name="Seun",
            phone_e164="+2348036667788",
            wa_id="2348036667788",
            drip_status=OutreachContact.DripStatus.MANUAL_FALLBACK,
        )
        process_webhook_payload(inbound_payload("2348036667788", "wamid.in.manual"), client=self.client_mock)
        contact.refresh_from_db()
        self.assertEqual(contact.drip_status, OutreachContact.DripStatus.REPLIED)
        self.client_mock.send_interactive_buttons.assert_not_called()

    def test_full_cloud_drip_reaches_step_3_automatically(self):
        t0 = timezone.now()
        contact = OutreachContact.objects.create(
            first_name="Ayo",
            phone_e164="+2347034445566",
            wa_id="2347034445566",
            drip_step=1,
            drip_status=OutreachContact.DripStatus.PENDING,
            next_send_at=t0 + timedelta(minutes=2),
        )
        # T0: prospect scans the QR -> Step 1 welcome
        process_webhook_payload(inbound_payload("2347034445566", "wamid.in.1"), client=self.client_mock)
        contact.refresh_from_db()
        self.assertEqual(contact.drip_step, 2)

        # +22h: Step 2 inside the free window
        result2 = self.router.execute_drip_step(contact, now=contact.next_send_at)
        self.assertEqual(result2["action"], "sent_interactive")
        self.assertEqual(result2["step"], 2)

        # They reply to Step 2, re-opening the window
        process_webhook_payload(inbound_payload("2347034445566", "wamid.in.2", "Amen"), client=self.client_mock)
        contact.refresh_from_db()
        self.assertEqual(contact.drip_status, OutreachContact.DripStatus.ACTIVE)

        # Step 3 is still free and gets sent
        result3 = self.router.execute_drip_step(contact, now=contact.next_send_at)
        contact.refresh_from_db()
        self.assertEqual(result3["action"], "sent_interactive")
        self.assertEqual(result3["step"], 3)
        self.assertEqual(contact.drip_status, OutreachContact.DripStatus.COMPLETED)
        self.assertEqual(self.client_mock.send_interactive_buttons.call_count, 3)
        self.client_mock.send_utility_template.assert_not_called()

    def test_reenabling_opted_out_contact_makes_it_due_again(self):
        contact = OutreachContact.objects.create(
            first_name="Dayo",
            phone_e164="+2348021112233",
            drip_status=OutreachContact.DripStatus.PENDING,
            next_send_at=timezone.now(),
        )
        user = get_user_model().objects.create_user(username="coord", password="pw-for-tests-123")
        web = Client()
        web.force_login(user)
        url = reverse("outreach:toggle_opt_out", args=[contact.pk])
        web.post(url)
        web.post(url)
        contact.refresh_from_db()

        self.assertFalse(contact.is_opted_out)
        self.assertEqual(contact.drip_status, OutreachContact.DripStatus.PENDING)
        self.assertIsNotNone(contact.next_send_at)
        with patch("outreach.tasks.execute_drip_step_task.delay"), self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(poll_due_drip_contacts()["claimed"], 1)


@override_settings(WA_CLOUD_API_ENABLED=True)
class DuplicateSendSafetyTests(TestCase):
    def setUp(self):
        self.client_mock = mock_meta_client()
        self.router = CostGuardRouter(client=self.client_mock)
        self.contact = OutreachContact.objects.create(
            first_name="Femi",
            phone_e164="+2348051234567",
            drip_step=1,
            drip_status=OutreachContact.DripStatus.PENDING,
            next_send_at=timezone.now() - timedelta(minutes=1),
        )

    def _claim(self):
        with patch("outreach.tasks.execute_drip_step_task.delay") as mock_delay, self.captureOnCommitCallbacks(
            execute=True
        ):
            poll_due_drip_contacts()
        return mock_delay.call_args.args

    def test_task_runs_once_and_redelivery_is_a_no_op(self):
        contact_id, lease = self._claim()
        with patch("outreach.services.MetaWhatsAppClient", return_value=self.client_mock):
            first = execute_drip_step_task(contact_id, lease)
            redelivered = execute_drip_step_task(contact_id, lease)

        self.assertEqual(first["action"], "sent_utility_template")
        self.assertEqual(redelivered, {"action": "skipped", "reason": "lease_superseded", "cost_usd": "0.0000"})
        self.client_mock.send_utility_template.assert_called_once()

    def test_send_in_progress_is_not_duplicated(self):
        WhatsAppMessage.objects.create(
            contact=self.contact,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            channel=WhatsAppMessage.Channel.CLOUD_API,
            message_type=WhatsAppMessage.MessageType.TEMPLATE,
            delivery_status=WhatsAppMessage.DeliveryStatus.QUEUED,
        )
        result = self.router.execute_drip_step(self.contact)
        self.assertEqual(result["reason"], "send_in_flight")
        self.client_mock.send_utility_template.assert_not_called()

    def test_stale_unconfirmed_send_goes_to_manual_queue_instead_of_resending(self):
        stale = WhatsAppMessage.objects.create(
            contact=self.contact,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            channel=WhatsAppMessage.Channel.CLOUD_API,
            message_type=WhatsAppMessage.MessageType.TEMPLATE,
            delivery_status=WhatsAppMessage.DeliveryStatus.QUEUED,
        )
        WhatsAppMessage.objects.filter(pk=stale.pk).update(created_at=timezone.now() - SEND_LEASE - timedelta(minutes=1))

        result = self.router.execute_drip_step(self.contact)
        self.contact.refresh_from_db()
        stale.refresh_from_db()

        self.assertEqual(result["reason"], "unconfirmed_previous_attempt")
        self.assertEqual(self.contact.drip_status, OutreachContact.DripStatus.MANUAL_FALLBACK)
        self.assertEqual(stale.delivery_status, WhatsAppMessage.DeliveryStatus.FAILED)
        self.assertEqual(stale.error_code, "UNCONFIRMED")
        self.client_mock.send_utility_template.assert_not_called()

    def test_network_error_records_one_failed_message_and_falls_back(self):
        self.client_mock.send_utility_template.side_effect = MetaCloudAPIError("timeout", error_code="NETWORK_ERROR")
        result = self.router.execute_drip_step(self.contact)
        self.contact.refresh_from_db()

        self.assertEqual(result["action"], "fallback_on_error")
        self.assertEqual(self.contact.drip_status, OutreachContact.DripStatus.MANUAL_FALLBACK)
        msg = WhatsAppMessage.objects.get(contact=self.contact)
        self.assertEqual(msg.delivery_status, WhatsAppMessage.DeliveryStatus.FAILED)
        self.assertEqual(msg.message_type, WhatsAppMessage.MessageType.TEMPLATE)
        self.assertEqual(msg.estimated_cost_usd, Decimal("0.0000"))

    def test_stop_received_during_send_is_not_overwritten(self):
        def send_and_opt_out(**kwargs):
            OutreachContact.objects.filter(pk=self.contact.pk).update(
                is_opted_out=True, drip_status=OutreachContact.DripStatus.OPTED_OUT, next_send_at=None
            )
            return {"messages": [{"id": "wamid.raced"}]}

        self.client_mock.send_utility_template.side_effect = send_and_opt_out
        self.router.execute_drip_step(self.contact)
        self.contact.refresh_from_db()

        self.assertTrue(self.contact.is_opted_out)
        self.assertEqual(self.contact.drip_status, OutreachContact.DripStatus.OPTED_OUT)
        self.assertIsNone(self.contact.next_send_at)
        self.assertEqual(WhatsAppMessage.objects.get(wamid="wamid.raced").delivery_status, "SENT")

    @override_settings(WA_MESSAGING_TIER_DAILY_LIMIT=0)
    def test_send_now_button_respects_daily_tier_limit(self):
        user = get_user_model().objects.create_user(username="coord", password="pw-for-tests-123")
        web = Client()
        web.force_login(user)
        with patch("outreach.services.MetaWhatsAppClient", return_value=self.client_mock):
            resp = web.post(reverse("outreach:trigger_step_now", args=[self.contact.pk]), HTTP_HX_REQUEST="true")
        self.assertContains(resp, "sending limit is reached")
        self.client_mock.send_utility_template.assert_not_called()

    def test_concurrent_duplicate_inbound_wamid_is_skipped_without_error(self):
        msg = {"from": "2348051234567", "id": "wamid.dup", "type": "text", "text": {"body": "Hi"}}
        self.assertTrue(_handle_inbound_message(self.contact, msg, self.router, self.client_mock))
        self.assertFalse(_handle_inbound_message(self.contact, msg, self.router, self.client_mock))
        self.assertEqual(WhatsAppMessage.objects.filter(wamid="wamid.dup").count(), 1)

    def test_concurrent_creation_of_new_inbound_contact_reuses_winner(self):
        # Another worker inserted this number between our lookups and our INSERT:
        # simulate by letting the first two lookups miss, so our create() hits the unique constraint.
        winner = OutreachContact.objects.create(first_name="Race", phone_e164="+2348077778888")
        real_filter = OutreachContact.objects.filter
        empty = OutreachContact.objects.none()
        lookups = iter([empty, empty])

        def racing_filter(*args, **kwargs):
            return next(lookups, None) or real_filter(*args, **kwargs)

        with patch.object(OutreachContact.objects, "filter", side_effect=racing_filter):
            contact = _get_or_create_inbound_contact("2348077778888", "Race Winner")

        self.assertEqual(contact.pk, winner.pk)
        self.assertEqual(OutreachContact.objects.filter(phone_e164="+2348077778888").count(), 1)


class ManualDoubleTapTests(TestCase):
    def test_double_tap_on_mark_sent_logs_step_once(self):
        contact = OutreachContact.objects.create(
            first_name="Segun",
            phone_e164="+2348145556677",
            drip_step=1,
            drip_status=OutreachContact.DripStatus.MANUAL_FALLBACK,
        )
        user = get_user_model().objects.create_user(username="coord", password="pw-for-tests-123")
        web = Client()
        web.force_login(user)
        url = reverse("outreach:mark_manual_sent", args=[contact.pk])

        web.post(url, {"step": 1}, HTTP_HX_REQUEST="true")
        second = web.post(url, {"step": 1}, HTTP_HX_REQUEST="true")
        contact.refresh_from_db()

        self.assertEqual(contact.drip_step, 2)
        self.assertContains(second, "already marked as sent")
        self.assertEqual(WhatsAppMessage.objects.filter(contact=contact).count(), 1)


@override_settings(WA_PHONE_NUMBER_ID="123", WA_ACCESS_TOKEN="token")
class MetaClientRetryPolicyTests(TestCase):
    def _resp(self, status: int, body: dict | None = None, headers: dict | None = None) -> MagicMock:
        resp = MagicMock(status_code=status, headers=headers or {}, text="")
        resp.json.return_value = body or {}
        return resp

    @patch("outreach.services.time.sleep")
    def test_429_is_retried_because_meta_did_not_process_it(self, mock_sleep):
        session = MagicMock()
        session.post.side_effect = [
            self._resp(429, {"error": {"code": 130429}}, {"Retry-After": "1"}),
            self._resp(200, {"messages": [{"id": "wamid.ok"}]}),
        ]
        data = MetaWhatsAppClient(session=session).send_text("2348031234567", "Hello")
        self.assertEqual(data["messages"][0]["id"], "wamid.ok")
        self.assertEqual(session.post.call_count, 2)
        mock_sleep.assert_called_once_with(1.0)

    def test_5xx_on_send_is_not_retried(self):
        session = MagicMock()
        session.post.return_value = self._resp(503, {"error": {"code": 2, "message": "Service unavailable"}})
        with self.assertRaises(MetaCloudAPIError) as ctx:
            MetaWhatsAppClient(session=session).send_text("2348031234567", "Hello")
        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(ctx.exception.status_code, 503)

    def test_read_timeout_becomes_network_error_not_a_crash(self):
        session = MagicMock()
        session.post.side_effect = requests.ReadTimeout("read timed out")
        with self.assertRaises(MetaCloudAPIError) as ctx:
            MetaWhatsAppClient(session=session).send_text("2348031234567", "Hello")
        self.assertEqual(ctx.exception.error_code, "NETWORK_ERROR")
        self.assertEqual(session.post.call_count, 1)

    def test_shared_session_only_auto_retries_get(self):
        from outreach.services import get_http_session

        retry = get_http_session().get_adapter("https://graph.facebook.com").max_retries
        self.assertNotIn("POST", retry.allowed_methods)
        self.assertIn("GET", retry.allowed_methods)
