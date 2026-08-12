# Copyright (c) 2026, Goodvantage GmbH and contributors

from __future__ import annotations

import hashlib
import hmac
from contextlib import nullcontext
from threading import Thread
from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import get_test_client

from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
	payrexx_settings as ps_module,
)
from payrexx_integration.payrexx_integration.doctype.payrexx_settings.test_payrexx_settings import (
	_create_test_user,
	_ensure_settings,
)
from payrexx_integration.payrexx_integration.payrexx import webhook_payload
from payrexx_integration.payrexx_integration.payrexx.payout_evidence import PAYOUT_EVIDENCE_DOCTYPE


def _payout(*, uuid: str = "AABB1122", mode: str = "TEST", status: str = "processing") -> dict:
	return {
		"uuid": uuid,
		"mode": mode,
		"object": "payout",
		"amount": 29390,
		"total_fees": 610,
		"currency": "CHF",
		"date": "2026-01-06",
		"statement": "Payrexx Demo Shop Thun",
		"payer": "payrexx",
		"status": status,
		"destination": {
			"type": "bank_account",
			"iban": "CH36 8914 4576 4981 8798 3",
			"account_holder": "Private Account Holder Must Not Persist",
		},
		"transfers": [
			{
				"type": "payout-fee",
				"amount": -10,
				"date_time": "2026-01-06T13:45:18+00:00",
				"items": [{"type": "payout-fee", "amount": -10}],
				"transaction": {},
			},
			{
				"type": "transaction",
				"amount": 29400,
				"date_time": "2025-12-25T13:44:30+00:00",
				"items": [
					{"type": "transaction", "amount": 30000},
					{"type": "transaction-fee", "amount": -600},
				],
				"transaction": {
					"type": "transaction",
					"amount": 30000,
					"uuid": "ba988b0c",
					"fee": 600,
					"currency": "EUR",
					"time": "2026-01-06T14:44:31+01:00",
					"payment": {"brand": "visa"},
					"reference_id": "PAYREXX-IR-TEST",
				},
			},
		],
		"merchant": {
			"id": "cedad42m",
			"name": "private-merchant-name",
			"site_title": "Private Site Title",
			"owner": {
				"company": "Private Merchant Company",
				"first_name": "Private",
				"last_name": "Owner",
				"address": "Private Street 1",
				"zip": "3000",
				"place": "Private Place",
				"email": "private-owner@example.test",
			},
		},
		"is_manual_payout": False,
	}


class TestPayoutWebhookShape(UnitTestCase):
	def test_only_the_documented_bare_payout_object_is_recognized(self):
		payout = _payout()
		self.assertIs(webhook_payload.payout_of(payout), payout)
		self.assertTrue(webhook_payload.is_payout_event(payout))
		self.assertFalse(webhook_payload.is_payout_event({"payout": payout}))
		self.assertFalse(webhook_payload.is_payout_event({"object": "Payout"}))


class TestPayoutWebhookCapture(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.automation_user = _create_test_user()
		self.settings_name = _ensure_settings(
			f"Payout-{frappe.generate_hash(length=8)}",
			automation_user=self.automation_user,
		)

	def _deliver(self, payload: dict, *, signature: str | None = None):
		body = frappe.as_json(payload).encode()
		if signature is None:
			signature = hmac.new(b"whk_test_dummy", body, hashlib.sha256).hexdigest()

		class FakeRequest:
			def __init__(self):
				self.args = {}
				self.content_type = "application/json"

			@staticmethod
			def get_data():
				return body

		original_request = getattr(frappe.local, "request", None)
		frappe.local.request = FakeRequest()
		try:
			with patch.object(frappe, "get_request_header", return_value=signature):
				return ps_module.callback(gateway_name=self.settings_name)
		finally:
			if original_request is None:
				delattr(frappe.local, "request")
			else:
				frappe.local.request = original_request

	def _evidence(self, *, uuid: str = "AABB1122", mode: str = "TEST"):
		return frappe.get_doc(
			PAYOUT_EVIDENCE_DOCTYPE,
			{"payrexx_settings": self.settings_name, "payout_uuid": uuid, "mode": mode},
		)

	def _deliver_failure_over_http(
		self,
		payload: dict,
		*,
		handler_name: str = "capture_payout_evidence",
		non_json_content_type: bool = False,
		sensitive_values: tuple[str, ...] | None = None,
	) -> dict:
		body = frappe.as_json(payload).encode()
		signature = hmac.new(b"whk_test_dummy", body, hashlib.sha256).hexdigest()
		if sensitive_values is None:
			sensitive_values = (
				payload["destination"]["iban"],
				payload["destination"]["account_holder"],
				payload["merchant"]["owner"]["email"],
			)
		result = {
			"capture_count": 0,
			"core_log_count": 0,
			"framework_snapshot_count": 0,
			"framework_snapshot_contains_pii": False,
			"framework_snapshot_excluded": False,
			"sanitized_log_count": 0,
			"sentry_count": 0,
		}
		site = frappe.local.site

		def capture_framework_snapshot(error):
			result["framework_snapshot_count"] += 1
			if isinstance(error, frappe.SecurityException):
				result["framework_snapshot_excluded"] = True
				return
			request_metadata = frappe.as_json(frappe.form_dict)
			result["framework_snapshot_contains_pii"] = any(
				value in request_metadata for value in sensitive_values
			)

		def post_webhook():
			try:
				settings = Mock(name="HTTP payout settings", get_password=Mock(return_value="whk_test_dummy"))
				settings.name = self.settings_name
				with (
					patch.object(ps_module, "_resolve_settings", return_value=settings),
					patch.object(ps_module, "as_automation_user", return_value=nullcontext()),
					patch.object(
						ps_module,
						handler_name,
						side_effect=RuntimeError("forced webhook processing failure"),
					) as capture,
					patch("frappe.app.log_error_snapshot", side_effect=capture_framework_snapshot),
					patch.object(frappe, "log_error") as core_log,
					patch.object(ps_module, "log_sanitized_error") as sanitized_log,
					patch("frappe.utils.sentry.capture_exception") as sentry,
				):
					result["response"] = get_test_client(use_cookies=False).post(
						f"/api/method/{ps_module.__name__}.callback?gateway_name={self.settings_name}",
						data=body,
						content_type=(
							"application/x-www-form-urlencoded"
							if non_json_content_type
							else "application/json"
						),
						headers={
							"X-Frappe-Site-Name": site,
							"X-Webhook-Signature": signature,
						},
					)
					result["capture_count"] = capture.call_count
					result["core_log_count"] = core_log.call_count
					result["sanitized_log_count"] = sanitized_log.call_count
					result["sentry_count"] = sentry.call_count
			except Exception as error:
				result["thread_error"] = error

		thread = Thread(target=post_webhook)
		thread.start()
		thread.join()
		return result

	def test_happy_capture_preserves_normalized_composition_under_the_automation_user(self):
		result = self._deliver(_payout())

		self.assertTrue(result["created"])
		self.assertFalse(result["status_changed"])
		evidence = self._evidence()
		self.assertEqual(evidence.owner, self.automation_user)
		self.assertEqual(evidence.amount, 29390)
		self.assertEqual(evidence.total_fees, 610)
		self.assertEqual(evidence.provider_status, "processing")
		self.assertEqual(evidence.evidence_origin, "Signed Provider Webhook")
		self.assertEqual(evidence.reconciliation_status, "Review")
		self.assertEqual(evidence.gross_amount, 30000)
		self.assertFalse(evidence.settlement_ready)
		self.assertEqual(len(evidence.transfers), 2)
		self.assertEqual(len(evidence.items), 3)
		self.assertEqual(evidence.transfers[1].transaction_uuid, "ba988b0c")
		self.assertEqual(evidence.transfers[1].reference_id, "PAYREXX-IR-TEST")
		self.assertEqual(
			[
				(item.transfer_index, item.provider_item_index, item.item_type, item.amount)
				for item in evidence.items
			],
			[
				(1, 1, "payout-fee", -10),
				(2, 1, "transaction", 30000),
				(2, 2, "transaction-fee", -600),
			],
		)

	def test_destination_and_merchant_pii_are_not_persisted(self):
		payload = _payout()
		self._deliver(payload)
		evidence = self._evidence()
		serialized = frappe.as_json(evidence.as_dict())

		for forbidden in (
			payload["destination"]["iban"],
			"CH3689144576498187983",
			payload["destination"]["account_holder"],
			payload["merchant"]["name"],
			payload["merchant"]["owner"]["first_name"],
			payload["merchant"]["owner"]["email"],
		):
			self.assertNotIn(forbidden, serialized)
		self.assertEqual(evidence.destination_iban_last_four, "7983")
		key = frappe.local.conf["encryption_key"].encode()
		expected_hash = hmac.new(key, b"CH3689144576498187983", hashlib.sha256).hexdigest()
		self.assertEqual(evidence.destination_iban_hash, expected_hash)

	def test_http_persistence_failure_never_reaches_framework_error_snapshot(self):
		result = self._deliver_failure_over_http(_payout(uuid="HTTPFAIL"))

		self.assertNotIn("thread_error", result)
		self.assertEqual(result["capture_count"], 1)
		self.assertEqual(result["core_log_count"], 0)
		self.assertFalse(result["framework_snapshot_contains_pii"])
		self.assertEqual(result["framework_snapshot_count"], 0)
		self.assertFalse(result["framework_snapshot_excluded"])
		self.assertEqual(result["sanitized_log_count"], 1)
		self.assertEqual(result["sentry_count"], 0)
		self.assertEqual(result["response"].status_code, 503)

	def test_http_transaction_failure_never_reaches_framework_error_snapshot(self):
		payer_email = "private-transaction-payer@example.test"
		result = self._deliver_failure_over_http(
			{
				"transaction": {
					"status": "waiting",
					"referenceId": "IR-PRIVATE-TRANSACTION",
					"invoice": {"contact": {"email": payer_email}},
				}
			},
			handler_name="_process_callback_transaction",
			sensitive_values=(payer_email,),
		)

		self.assertNotIn("thread_error", result)
		self.assertEqual(result["capture_count"], 1)
		self.assertEqual(result["core_log_count"], 0)
		self.assertFalse(result["framework_snapshot_contains_pii"])
		self.assertEqual(result["framework_snapshot_count"], 0)
		self.assertEqual(result["sanitized_log_count"], 1)
		self.assertEqual(result["sentry_count"], 0)
		self.assertEqual(result["response"].status_code, 503)

	def test_http_subscription_failure_never_reaches_framework_error_snapshot(self):
		contact_email = "private-subscription-contact@example.test"
		result = self._deliver_failure_over_http(
			{
				"id": 42,
				"status": "active",
				"paymentInterval": "P1M",
				"contact": {"email": contact_email},
			},
			handler_name="_process_callback_subscription",
			sensitive_values=(contact_email,),
		)

		self.assertNotIn("thread_error", result)
		self.assertEqual(result["capture_count"], 1)
		self.assertEqual(result["core_log_count"], 0)
		self.assertFalse(result["framework_snapshot_contains_pii"])
		self.assertEqual(result["framework_snapshot_count"], 0)
		self.assertEqual(result["sanitized_log_count"], 1)
		self.assertEqual(result["sentry_count"], 0)
		self.assertEqual(result["response"].status_code, 503)

	def test_http_signed_non_json_payout_never_reaches_framework_error_snapshot(self):
		result = self._deliver_failure_over_http(
			_payout(uuid="HTTPPRE"),
			non_json_content_type=True,
		)

		self.assertNotIn("thread_error", result)
		self.assertEqual(result["capture_count"], 0)
		self.assertEqual(result["core_log_count"], 0)
		self.assertFalse(result["framework_snapshot_contains_pii"])
		self.assertIn(result["framework_snapshot_count"], (0, 1))
		self.assertEqual(
			result["framework_snapshot_excluded"],
			bool(result["framework_snapshot_count"]),
		)
		self.assertEqual(result["sanitized_log_count"], 0)
		self.assertEqual(result["sentry_count"], 0)
		self.assertEqual(result["response"].status_code, 417)

	def test_exact_same_state_replay_does_not_duplicate_or_modify_evidence(self):
		first = self._deliver(_payout())
		evidence = self._evidence()
		modified = evidence.modified
		received_on = evidence.received_on

		second = self._deliver(_payout())

		self.assertEqual(second["payout_evidence"], first["payout_evidence"])
		self.assertFalse(second["created"])
		self.assertFalse(second["status_changed"])
		self.assertEqual(
			frappe.db.count(
				PAYOUT_EVIDENCE_DOCTYPE,
				{"payrexx_settings": self.settings_name, "payout_uuid": "AABB1122", "mode": "TEST"},
			),
			1,
		)
		evidence.reload()
		self.assertEqual(evidence.modified, modified)
		self.assertEqual(evidence.received_on, received_on)

	def test_processing_may_progress_to_sent_or_failed_only(self):
		self._deliver(_payout(uuid="STATUS01"))
		result = self._deliver(_payout(uuid="STATUS01", status="sent"))
		self.assertTrue(result["status_changed"])
		evidence = self._evidence(uuid="STATUS01")
		self.assertEqual(evidence.provider_status, "sent")
		self.assertTrue(evidence.settlement_ready)

		with self.assertRaisesRegex(frappe.ValidationError, "only progress from processing"):
			self._deliver(_payout(uuid="STATUS01", status="failed"))
		evidence.reload()
		self.assertEqual(evidence.provider_status, "sent")

		self._deliver(_payout(uuid="STATUS02"))
		self._deliver(_payout(uuid="STATUS02", status="failed"))
		failed = self._evidence(uuid="STATUS02")
		self.assertEqual(failed.provider_status, "failed")
		self.assertFalse(failed.settlement_ready)

	def test_preliminary_status_is_captured_but_never_settlement_ready_or_advanced(self):
		self._deliver(_payout(uuid="PRELIM01", status="under-review"))
		evidence = self._evidence(uuid="PRELIM01")
		self.assertFalse(evidence.settlement_ready)
		with self.assertRaisesRegex(frappe.ValidationError, "only progress from processing"):
			self._deliver(_payout(uuid="PRELIM01", status="processing"))
		evidence.reload()
		self.assertEqual(evidence.provider_status, "under-review")

	def test_composition_mutation_for_the_same_evidence_key_fails_closed(self):
		self._deliver(_payout())
		mutated = _payout()
		mutated["amount"] += 1
		mutated["transfers"][1]["amount"] += 1
		mutated["transfers"][1]["items"][0]["amount"] += 1

		with self.assertRaisesRegex(frappe.ValidationError, "composition changed"):
			self._deliver(mutated)
		evidence = self._evidence()
		self.assertEqual(evidence.amount, 29390)

	def test_invalid_arithmetic_and_minor_unit_types_are_rejected(self):
		wrong_payout_total = _payout(uuid="INVALID1")
		wrong_payout_total["amount"] += 1
		wrong_transfer_total = _payout(uuid="INVALID2")
		wrong_transfer_total["transfers"][1]["amount"] += 1

		for payload, message in (
			(wrong_payout_total, "sum of transfer amounts"),
			(wrong_transfer_total, "sum of its item amounts"),
		):
			with (
				self.subTest(message=message),
				patch.object(frappe, "log_error") as log_error,
				self.assertRaisesRegex(frappe.ValidationError, message),
			):
				self._deliver(payload)
			log_error.assert_not_called()

		for index, invalid_amount in enumerate((True, 29390.0, "29390"), start=3):
			payload = _payout(uuid=f"INVALID{index}")
			payload["amount"] = invalid_amount
			with (
				self.subTest(value=invalid_amount),
				self.assertRaisesRegex(frappe.ValidationError, "integer in provider minor units"),
			):
				self._deliver(payload)

		self.assertEqual(
			frappe.db.count(PAYOUT_EVIDENCE_DOCTYPE, {"payrexx_settings": self.settings_name}),
			0,
		)

	def test_unknown_status_is_rejected(self):
		with self.assertRaisesRegex(frappe.ValidationError, "unsupported value"):
			self._deliver(_payout(status="paid"))
		self.assertEqual(
			frappe.db.count(PAYOUT_EVIDENCE_DOCTYPE, {"payrexx_settings": self.settings_name}),
			0,
		)

	def test_signed_callback_rejects_reserved_synthetic_uuid_and_status(self):
		for fieldname, value in (("uuid", "SYNTHETIC-reserved"), ("status", "synthetic")):
			payload = _payout(uuid=f"RESERVED-{fieldname}")
			payload[fieldname] = value
			with self.subTest(fieldname=fieldname), self.assertRaises(frappe.ValidationError):
				self._deliver(payload)
		self.assertEqual(
			frappe.db.count(PAYOUT_EVIDENCE_DOCTYPE, {"payrexx_settings": self.settings_name}),
			0,
		)

	def test_same_uuid_in_test_and_live_modes_has_separate_evidence(self):
		test_result = self._deliver(_payout(mode="TEST"))
		live_result = self._deliver(_payout(mode="LIVE"))

		self.assertNotEqual(test_result["payout_evidence"], live_result["payout_evidence"])
		self.assertEqual(
			frappe.db.count(
				PAYOUT_EVIDENCE_DOCTYPE,
				{"payrexx_settings": self.settings_name, "payout_uuid": "AABB1122"},
			),
			2,
		)
		self.assertEqual(
			set(
				frappe.get_all(
					PAYOUT_EVIDENCE_DOCTYPE,
					filters={"payrexx_settings": self.settings_name, "payout_uuid": "AABB1122"},
					pluck="reconciliation_status",
				)
			),
			{"Review"},
		)

	def test_invalid_signature_is_side_effect_free(self):
		with (
			patch.object(ps_module, "capture_payout_evidence") as capture,
			self.assertRaises(frappe.AuthenticationError),
		):
			self._deliver(_payout(), signature="invalid")

		capture.assert_not_called()
		self.assertEqual(
			frappe.db.count(PAYOUT_EVIDENCE_DOCTYPE, {"payrexx_settings": self.settings_name}),
			0,
		)

	def test_transaction_shape_still_uses_the_existing_callback_path(self):
		transaction = {"status": "waiting", "referenceId": "IR-UNCHANGED"}
		with patch.object(ps_module, "_process_callback_transaction", return_value={"ok": True}) as process:
			self.assertEqual(self._deliver({"transaction": transaction}), {"ok": True})
		process.assert_called_once_with(self.settings_name, transaction, "IR-UNCHANGED", "waiting")

	def test_subscription_shape_still_uses_the_existing_callback_path(self):
		subscription = {"id": 42, "status": "active", "paymentInterval": "P1M"}
		with patch.object(ps_module, "_process_callback_subscription", return_value={"ok": True}) as process:
			self.assertEqual(self._deliver(subscription), {"ok": True})
		process.assert_called_once_with(self.settings_name, subscription)
