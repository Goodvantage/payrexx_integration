from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import nowdate

from payrexx_integration.api import _sign, pay_invoice
from payrexx_integration.hosted_qa import _validate_invoice, inspect_settlement, preflight
from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
	payrexx_settings as payrexx_settings_module,
)
from payrexx_integration.payrexx_integration.doctype.payrexx_settings.test_payrexx_settings import (
	_create_submitted_test_sales_invoice,
	_ensure_settings,
	_ensure_test_payment_gateway_account,
)
from payrexx_integration.tests.hosted_settlement_qa import (
	_state_record_names,
	_validated_base_url,
	_write_state,
)

RUN_ID = f"PRX-SBX-E2E-{nowdate().replace('-', '')}-deadbeef"


class TestHostedPayrexxQA(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.settings_name = _ensure_settings()
		self.invoice = _create_submitted_test_sales_invoice()
		self.gateway = f"Payrexx-{self.settings_name}"
		_ensure_test_payment_gateway_account(self.gateway, self.invoice)

	@contextmanager
	def configured(self, *, enabled: int = 1):
		values = {
			"developer_mode": 1,
			"payrexx_hosted_qa_enabled": enabled,
			"payrexx_hosted_qa_gateway": self.settings_name,
			"payrexx_hosted_qa_invoice": self.invoice.name,
			"host_name": "https://qa.example.test",
		}
		original = {key: frappe.conf.get(key) for key in values}
		try:
			frappe.conf.update(values)
			yield
		finally:
			for key, value in original.items():
				if value is None:
					frappe.conf.pop(key, None)
				else:
					frappe.conf[key] = value

	def create_checkout(self):
		client = Mock()
		client.create_gateway.return_value = {
			"id": 424242,
			"hash": "sandbox-hash",
			"link": "https://sandbox.payrexx.example/checkout?secret=value",
		}
		original_response = getattr(frappe.local, "response", None)
		original_commit = getattr(frappe.local.flags, "commit", False)
		try:
			frappe.local.response = {}
			frappe.local.flags.commit = False
			with patch.object(payrexx_settings_module.PayrexxSettings, "_client", return_value=client):
				pay_invoice(
					si=self.invoice.name,
					token=_sign(self.invoice.name, self.settings_name),
					gateway_name=self.settings_name,
				)
		finally:
			frappe.local.response = original_response or {}
			frappe.local.flags.commit = original_commit

		payment_request_name = frappe.get_all(
			"Payment Request",
			filters={
				"reference_doctype": "Sales Invoice",
				"reference_name": self.invoice.name,
				"payment_gateway": self.gateway,
			},
			pluck="name",
		)[0]
		integration_request_name = frappe.get_all(
			"Integration Request",
			filters={
				"reference_doctype": "Payment Request",
				"reference_docname": payment_request_name,
				"integration_request_service": "Payrexx",
			},
			pluck="name",
		)[0]
		return payment_request_name, integration_request_name

	def test_preflight_fails_closed_when_disabled(self):
		with self.configured(enabled=0), self.assertRaises(frappe.PermissionError):
			preflight(RUN_ID)

	def test_preflight_requires_accounts_manager(self):
		with (
			self.configured(),
			patch("payrexx_integration.hosted_qa.frappe.get_roles", return_value=["System Manager"]),
			self.assertRaises(frappe.PermissionError),
		):
			preflight(RUN_ID)

	def test_preflight_rejects_invalid_run_marker(self):
		with self.configured(), self.assertRaises(frappe.ValidationError):
			preflight("unsafe-run")

	def test_preflight_rejects_stale_run_marker(self):
		with self.configured(), self.assertRaises(frappe.ValidationError):
			preflight("PRX-SBX-E2E-20200101-deadbeef")

	def test_preflight_is_read_only_before_checkout(self):
		with (
			self.configured(),
			patch.object(payrexx_settings_module.PayrexxSettings, "_ping") as ping,
		):
			result = preflight(RUN_ID)

		self.assertEqual(result["stage"], "ready_for_checkout")
		self.assertIsNone(result["payment_request"])
		self.assertIsNone(result["integration_request"])
		self.assertFalse(result["checkout_present"])
		self.assertEqual(
			frappe.get_all(
				"Payment Request",
				filters={"reference_doctype": "Sales Invoice", "reference_name": self.invoice.name},
			),
			[],
		)
		ping.assert_called_once()

	def test_preflight_resumes_one_existing_checkout_without_exposing_url(self):
		payment_request, integration_request = self.create_checkout()
		with (
			self.configured(),
			patch.object(payrexx_settings_module.PayrexxSettings, "_ping"),
		):
			result = preflight(RUN_ID)

		self.assertEqual(result["stage"], "awaiting_payment")
		self.assertEqual(result["payment_request"], payment_request)
		self.assertEqual(result["integration_request"], integration_request)
		self.assertTrue(result["checkout_present"])
		serialized = json.dumps(result).lower()
		self.assertNotIn("sandbox.payrexx.example", serialized)
		self.assertNotIn("payment_url", serialized)
		self.assertNotIn("checkout_url", serialized)

	def test_preflight_rejects_failed_integration_request(self):
		_payment_request, integration_request = self.create_checkout()
		frappe.db.set_value("Integration Request", integration_request, "status", "Failed")
		with (
			self.configured(),
			patch.object(payrexx_settings_module.PayrexxSettings, "_ping"),
			self.assertRaises(frappe.ValidationError),
		):
			preflight(RUN_ID)

	def test_inspector_reports_pending_without_reconciling(self):
		payment_request, integration_request = self.create_checkout()
		with (
			self.configured(),
			patch.object(payrexx_settings_module, "reconcile_integration_request") as reconcile,
		):
			result = inspect_settlement(RUN_ID, payment_request, integration_request)

		self.assertFalse(result["settled"])
		self.assertFalse(result["checks"]["integration_request_completed"])
		self.assertFalse(result["checks"]["payment_request_paid"])
		reconcile.assert_not_called()

	def test_inspector_proves_exact_test_mode_settlement_chain(self):
		payment_request_name, integration_request_name = self.create_checkout()
		payment_request = frappe.get_doc("Payment Request", payment_request_name)
		transaction = {
			"id": 54321,
			"status": "confirmed",
			"mode": "TEST",
			"amount": round(payment_request.grand_total * 100),
			"invoice": {
				"referenceId": integration_request_name,
				"currency": payment_request.currency,
			},
		}
		payrexx_settings_module._complete_integration_request(integration_request_name, transaction)

		with self.configured():
			result = inspect_settlement(RUN_ID, payment_request_name, integration_request_name)

		self.assertTrue(result["settled"])
		self.assertTrue(all(result["checks"].values()))
		self.assertEqual(result["provider_mode"], "TEST")
		self.assertEqual(len(result["payment_entries"]), 1)

	def test_inspector_rejects_live_provider_mode(self):
		payment_request_name, integration_request_name = self.create_checkout()
		payment_request = frappe.get_doc("Payment Request", payment_request_name)
		transaction = {
			"id": 54321,
			"status": "confirmed",
			"mode": "LIVE",
			"amount": round(payment_request.grand_total * 100),
			"invoice": {
				"referenceId": integration_request_name,
				"currency": payment_request.currency,
			},
		}
		payrexx_settings_module._complete_integration_request(integration_request_name, transaction)

		with self.configured():
			result = inspect_settlement(RUN_ID, payment_request_name, integration_request_name)

		self.assertFalse(result["settled"])
		self.assertFalse(result["checks"]["provider_transaction_test_mode"])

	def test_inspector_rejects_manual_payment_entry_not_created_by_payrexx(self):
		payment_request_name, integration_request_name = self.create_checkout()
		payment_entry = get_payment_entry("Sales Invoice", self.invoice.name)
		payment_entry.reference_no = "MANUAL-PAYMENT"
		payment_entry.reference_date = nowdate()
		payment_entry.insert(ignore_permissions=True)
		payment_entry.submit()
		self.assertIn(
			payment_request_name,
			{row.payment_request for row in payment_entry.references if row.payment_request},
		)

		payment_request = frappe.get_doc("Payment Request", payment_request_name)
		transaction = {
			"id": 54321,
			"status": "confirmed",
			"mode": "TEST",
			"amount": round(payment_request.grand_total * 100),
			"invoice": {
				"referenceId": integration_request_name,
				"currency": payment_request.currency,
			},
		}
		integration_request = frappe.get_doc("Integration Request", integration_request_name)
		request_data = frappe.parse_json(integration_request.data) or {}
		request_data["payrexx_transaction"] = transaction
		integration_request.db_set(
			{"status": "Completed", "data": frappe.as_json(request_data)},
			update_modified=False,
		)

		with self.configured():
			result = inspect_settlement(RUN_ID, payment_request_name, integration_request_name)

		self.assertFalse(result["settled"])
		self.assertFalse(result["checks"]["payment_entry_recorded_by_payrexx"])
		self.assertFalse(result["checks"]["payment_entry_reference_number_exact"])


class TestHostedSettlementRunner(UnitTestCase):
	def test_invoice_validation_uses_erpnext_rounded_payable_total(self):
		invoice = frappe._dict(
			docstatus=1,
			is_return=0,
			outstanding_amount=356.75,
			grand_total=356.73,
			rounded_total=356.75,
			currency="CHF",
		)
		settings = Mock()
		settings.get_password.return_value = "configured"

		_validate_invoice(invoice, settings)

		settings.validate_transaction_currency.assert_called_once_with("CHF")

	def test_base_url_requires_exact_allowlisted_https_origin(self):
		self.assertEqual(
			_validated_base_url("https://qa.example.test", "qa.example.test"),
			"https://qa.example.test",
		)
		for value in (
			"http://qa.example.test",
			"https://qa.example.test/path",
			"https://qa.example.test?token=secret",
			"https://other.example.test",
		):
			with self.subTest(value=value), self.assertRaises(ValueError):
				_validated_base_url(value, "qa.example.test")

	def test_state_writer_rejects_sensitive_fields_and_uses_owner_only_mode(self):
		with TemporaryDirectory() as directory:
			path = Path(directory) / "state.json"
			_write_state(path, {"invoice": "SINV-1", "checkout_present": True})
			self.assertEqual(path.stat().st_mode & 0o777, 0o600)
			with self.assertRaises(RuntimeError):
				_write_state(path, {"payment_url": "https://provider.example/?secret=1"})

	def test_state_record_names_require_matching_run(self):
		state = {
			"run_id": "PRX-SBX-E2E-20260722-deadbeef",
			"payment_request": "PR-1",
			"integration_request": "IR-1",
		}
		self.assertEqual(
			_state_record_names(state, state["run_id"]),
			("PR-1", "IR-1"),
		)
		with self.assertRaises(RuntimeError):
			_state_record_names(state, "PRX-SBX-E2E-20260722-feedface")
