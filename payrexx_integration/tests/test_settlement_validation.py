from contextlib import nullcontext
from unittest.mock import Mock, patch

import frappe
from frappe.tests import UnitTestCase

from payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings import (
	CHARGEBACK_ERROR,
	_confirmed_transaction_from_gateway,
	_is_chargeback_recorded,
	_mark_reconciliation_failure,
	_settlement_conflict,
	reconcile_integration_request,
)


class TestSettlementValidation(UnitTestCase):
	def test_chargeback_terminal_evidence_accepts_transaction_or_error_marker(self):
		self.assertTrue(
			_is_chargeback_recorded(
				frappe._dict(data="{}", error=""),
				{"payrexx_transaction": {"status": "chargeback"}},
			)
		)

	def test_reconciliation_failure_rechecks_chargeback_under_row_lock(self):
		integration_request = frappe._dict(
			name="IR-CHARGEBACK",
			status="Failed",
			error=CHARGEBACK_ERROR,
			data=frappe.as_json({"payrexx_transaction": {"status": "chargeback"}}),
			save=Mock(),
		)
		with patch(
			"payrexx_integration.payrexx_integration.doctype.payrexx_settings."
			"payrexx_settings.frappe.get_doc",
			return_value=integration_request,
		) as get_current_request:
			_mark_reconciliation_failure(integration_request.name, "declined")

		get_current_request.assert_called_once_with(
			"Integration Request",
			integration_request.name,
			for_update=True,
		)
		integration_request.save.assert_not_called()
		self.assertTrue(_is_chargeback_recorded(frappe._dict(data="{}", error=CHARGEBACK_ERROR), {}))
		self.assertFalse(
			_is_chargeback_recorded(
				frappe._dict(data="{}", error="Payrexx status: declined"),
				{"payrexx_transaction": {"status": "confirmed"}},
			)
		)

	@staticmethod
	def _patched_get_value(*, allow_test_transactions: int):
		"""Answer the gateway opt-in lookup; leave the currency fraction unit alone."""

		def get_value(doctype, *args, **kwargs):
			if doctype == "Payrexx Settings":
				return allow_test_transactions
			return 100

		return patch(
			"payrexx_integration.payrexx_integration.doctype.payrexx_settings."
			"payrexx_settings.frappe.db.get_value",
			side_effect=get_value,
		)

	def assertPassedTestModeGate(self, conflict):
		"""The mode gate let this through; later checks are another test's business.

		These stubs carry no Payment Request reference, so a transaction that
		clears the gate still stops at `payment_request_reference_required`.
		"""
		if conflict is not None:
			self.assertNotEqual(conflict["code"], "test_transaction")

	def test_test_mode_transaction_is_rejected_unless_the_gateway_opts_in(self):
		"""A simulated payment matches every other check, so mode is the only thing stopping it."""
		integration_request = frappe._dict(reference_doctype=None)
		ir_data = {"amount": 100, "currency": "CHF", "payrexx_settings": "Live"}
		confirmed_test_payment = {
			"status": "confirmed",
			"mode": "TEST",
			"amount": 10000,
			"currency": "CHF",
		}
		with self._patched_get_value(allow_test_transactions=0):
			self.assertEqual(
				_settlement_conflict(integration_request, ir_data, confirmed_test_payment)["code"],
				"test_transaction",
			)

		# Same payment, sandbox gateway: the opt-in lets it through to the
		# ordinary amount/currency checks, which it passes.
		with self._patched_get_value(allow_test_transactions=1):
			self.assertPassedTestModeGate(
				_settlement_conflict(integration_request, ir_data, confirmed_test_payment)
			)

	def test_live_transaction_is_unaffected_by_the_gate(self):
		integration_request = frappe._dict(reference_doctype=None)
		ir_data = {"amount": 100, "currency": "CHF", "payrexx_settings": "Live"}
		with self._patched_get_value(allow_test_transactions=0):
			self.assertPassedTestModeGate(
				_settlement_conflict(
					integration_request,
					ir_data,
					{"status": "confirmed", "mode": "LIVE", "amount": 10000, "currency": "CHF"},
				)
			)

	def test_invoice_test_flag_is_honoured_when_mode_is_absent(self):
		integration_request = frappe._dict(reference_doctype=None)
		ir_data = {"amount": 100, "currency": "CHF", "payrexx_settings": "Live"}
		with self._patched_get_value(allow_test_transactions=0):
			self.assertEqual(
				_settlement_conflict(
					integration_request,
					ir_data,
					{"status": "confirmed", "amount": 10000, "invoice": {"currency": "CHF", "test": 1}},
				)["code"],
				"test_transaction",
			)
			# Neither marker present: undecidable, so settlement proceeds exactly
			# as it did before the gate existed.
			self.assertPassedTestModeGate(
				_settlement_conflict(
					integration_request,
					ir_data,
					{"status": "confirmed", "amount": 10000, "currency": "CHF"},
				)
			)

	def test_browser_reconciliation_preserves_parent_invoice_test_evidence(self):
		integration_request = frappe._dict(reference_doctype=None)
		ir_data = {"amount": 100, "currency": "CHF", "payrexx_settings": "Live"}
		gateway = {
			"invoices": [
				{
					"referenceId": "IR-TEST",
					"currency": "CHF",
					"test": 1,
					"transactions": [{"id": 1, "status": "confirmed", "amount": 10000}],
				}
			]
		}

		transaction = _confirmed_transaction_from_gateway(gateway, "IR-TEST")
		with self._patched_get_value(allow_test_transactions=0):
			self.assertEqual(
				_settlement_conflict(integration_request, ir_data, transaction)["code"],
				"test_transaction",
			)

	def test_confirmation_requires_provider_and_requested_amount_currency(self):
		integration_request = frappe._dict(reference_doctype=None)
		self.assertEqual(
			_settlement_conflict(
				integration_request,
				{"amount": 100, "currency": "CHF"},
				{"status": "confirmed"},
			)["code"],
			"provider_evidence_missing",
		)
		self.assertEqual(
			_settlement_conflict(
				integration_request,
				{},
				{"status": "confirmed", "amount": 10000, "currency": "CHF"},
			)["code"],
			"checkout_evidence_missing",
		)

	def test_confirmation_rejects_provider_amount_or_currency_mismatch(self):
		integration_request = frappe._dict(reference_doctype=None)
		with patch(
			"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.frappe.db.get_value",
			return_value=100,
		):
			self.assertEqual(
				_settlement_conflict(
					integration_request,
					{"amount": 100, "currency": "CHF"},
					{"amount": 9999, "currency": "CHF"},
				)["code"],
				"amount_mismatch",
			)
			self.assertEqual(
				_settlement_conflict(
					integration_request,
					{"amount": 100, "currency": "CHF"},
					{"amount": 10000, "currency": "EUR"},
				)["code"],
				"currency_mismatch",
			)

	def test_confirmation_rejects_payment_request_changed_by_another_channel(self):
		integration_request = frappe._dict(
			reference_doctype="Payment Request",
			reference_docname="PR-TEST",
		)
		payment_request = frappe._dict(
			name="PR-TEST",
			docstatus=1,
			status="Partially Paid",
			payment_request_type="Inward",
			outstanding_amount=50,
			grand_total=100,
			currency="CHF",
			reference_doctype="Sales Invoice",
			reference_name="SINV-TEST",
			precision=lambda _fieldname: 2,
		)

		def get_value(doctype, name, fieldname, **kwargs):
			if doctype == "Currency":
				return 100
			if kwargs.get("for_update"):
				return name
			if doctype == "Sales Invoice":
				return frappe._dict(outstanding_amount=50, currency="CHF")
			raise AssertionError((doctype, name, fieldname, kwargs))

		with (
			patch(
				"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.frappe.get_doc",
				return_value=payment_request,
			),
			patch(
				"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.frappe.db.exists",
				return_value=True,
			),
			patch(
				"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.frappe.db.get_value",
				side_effect=get_value,
			),
		):
			reason = _settlement_conflict(
				integration_request,
				{"amount": 100, "currency": "CHF"},
				{"amount": 10000, "currency": "CHF"},
			)

		self.assertEqual(reason["code"], "payment_request_not_active")

	def test_confirmation_rejects_unsupported_payment_request_source(self):
		integration_request = frappe._dict(
			reference_doctype="Payment Request",
			reference_docname="PR-SALES-ORDER",
		)
		payment_request = frappe._dict(
			name="PR-SALES-ORDER",
			docstatus=1,
			status="Requested",
			payment_request_type="Inward",
			outstanding_amount=100,
			grand_total=100,
			currency="CHF",
			reference_doctype="Sales Order",
			reference_name="SO-TEST",
		)
		with (
			patch(
				"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.frappe.get_doc",
				return_value=payment_request,
			),
			patch(
				"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.frappe.db.exists",
				return_value=True,
			),
			patch(
				"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.frappe.db.get_value",
				return_value="PR-SALES-ORDER",
			),
		):
			reason = _settlement_conflict(
				integration_request,
				{"payrexx_gateway_amount": 10000, "payrexx_gateway_currency": "CHF"},
				{"status": "confirmed", "amount": 10000, "currency": "CHF"},
			)

		self.assertEqual(reason["code"], "unsupported_source_doctype")

	def test_success_fallback_rejects_confirmed_gateway_without_confirmed_transaction(self):
		integration_request = frappe._dict(
			name="IR-TEST",
			integration_request_service="Payrexx",
			status="Queued",
			data=frappe.as_json({"payrexx_gateway_id": 123, "amount": 100, "currency": "CHF"}),
		)
		client = frappe._dict(
			retrieve_gateway=lambda _gateway_id: {
				"status": "confirmed",
				"amount": 10000,
				"currency": "CHF",
				"invoices": [],
			}
		)
		settings = frappe._dict(name="Live", _client=lambda: client)
		with (
			patch(
				"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.frappe.db.exists",
				return_value=True,
			),
			patch(
				"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.frappe.get_doc",
				return_value=integration_request,
			),
			patch(
				"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings._resolve_settings",
				return_value=settings,
			),
			patch(
				"payrexx_integration.payrexx_integration.doctype.payrexx_settings."
				"payrexx_settings._multiple_gateways_configured",
				return_value=False,
			),
			patch(
				"payrexx_integration.payrexx_integration.doctype.payrexx_settings."
				"payrexx_settings._payment_authorization_user",
				return_value=nullcontext(),
			),
			patch(
				"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings._complete_integration_request"
			) as complete,
		):
			self.assertFalse(reconcile_integration_request(integration_request.name))

		complete.assert_not_called()

	def test_completed_request_requires_stored_confirmed_transaction(self):
		integration_request = frappe._dict(
			name="IR-COMPLETED-WITHOUT-TRANSACTION",
			integration_request_service="Payrexx",
			status="Completed",
			data="{}",
		)
		with (
			patch(
				"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.frappe.db.exists",
				return_value=True,
			),
			patch(
				"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.frappe.get_doc",
				return_value=integration_request,
			),
			patch(
				"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings._resolve_settings"
			) as resolve_settings,
		):
			self.assertFalse(reconcile_integration_request(integration_request.name))

		resolve_settings.assert_not_called()

	def test_success_fallback_does_not_retrieve_terminal_conflict_again(self):
		integration_request = frappe._dict(
			name="IR-TERMINAL",
			integration_request_service="Payrexx",
			status="Failed",
			data=frappe.as_json(
				{
					"payrexx_gateway_id": 123,
					"payrexx_settlement_conflict": {
						"version": 1,
						"terminal": True,
						"code": "amount_mismatch",
					},
				}
			),
		)
		with (
			patch(
				"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.frappe.db.exists",
				return_value=True,
			),
			patch(
				"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings.frappe.get_doc",
				return_value=integration_request,
			),
			patch(
				"payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings._resolve_settings"
			) as resolve_settings,
		):
			self.assertFalse(reconcile_integration_request(integration_request.name))

		resolve_settings.assert_not_called()
