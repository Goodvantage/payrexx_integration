# Copyright (c) 2026, Goodvantage GmbH and contributors

"""Provider-side refunds and disputes.

Refunds are issued in the Payrexx dashboard, never from ERPNext. These tests pin
the contract that follows from that: the reversal is recorded as evidence beside
the settlement, put in front of accounting, and posted to no ledger.
"""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
	payrexx_settings as ps_module,
)
from payrexx_integration.payrexx_integration.doctype.payrexx_settings.test_payrexx_settings import (
	GATEWAY_NAME,
	_ensure_settings,
)


def _reversals(integration_request_name: str) -> list[dict]:
	data = frappe.parse_json(frappe.db.get_value("Integration Request", integration_request_name, "data"))
	return (data or {}).get(ps_module.REVERSAL_DATA_KEY) or []


def _todos(integration_request_name: str, marker: str) -> list[str]:
	return frappe.get_all(
		"ToDo",
		filters={
			"reference_type": "Integration Request",
			"reference_name": integration_request_name,
			"description": ["like", f"{marker}%"],
		},
		pluck="description",
	)


class TestProviderReversals(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.settings_name = _ensure_settings()

	def _settled_request(self) -> str:
		"""A Completed Integration Request carrying its confirmed settlement evidence."""
		confirmed = {"id": 900, "uuid": "txn-confirmed", "status": "confirmed", "amount": 10000}
		return (
			frappe.get_doc(
				{
					"doctype": "Integration Request",
					"integration_request_service": "Payrexx",
					"status": "Completed",
					"data": frappe.as_json(
						{"payrexx_settings": self.settings_name, "payrexx_transaction": confirmed}
					),
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _deliver(self, name: str, transaction: dict) -> None:
		self.assertEqual(
			ps_module._process_callback_transaction(
				self.settings_name, transaction, name, transaction["status"]
			),
			{"ok": True},
		)

	def test_refund_of_a_settled_payment_is_recorded_and_raised_to_accounting(self):
		name = self._settled_request()
		self._deliver(
			name,
			{
				"id": 901,
				"uuid": "txn-refund-1",
				"status": "refunded",
				"amount": 10000,
				"originalTransactionId": 900,
				"invoice": {"currency": "CHF"},
			},
		)

		reversal = self.assertOneReversal(name)
		self.assertEqual(reversal["status"], "refunded")
		self.assertEqual(reversal["amount"], 10000)
		self.assertEqual(reversal["currency"], "CHF")
		self.assertEqual(reversal["original_transaction"], 900)

		# The settlement evidence survives: it is what was collected.
		data = frappe.parse_json(frappe.db.get_value("Integration Request", name, "data"))
		self.assertEqual(data["payrexx_transaction"]["status"], "confirmed")
		# A refunded payment did settle; rewriting history to Failed would lie.
		self.assertEqual(frappe.db.get_value("Integration Request", name, "status"), "Completed")

		todos = _todos(name, ps_module.REFUND_TODO_MARKER)
		self.assertEqual(len(todos), 1)
		self.assertIn("CHF 100.00", todos[0])

	def test_replayed_refund_delivery_records_once(self):
		"""Payrexx retries up to ten times; a slow success looks like a failure."""
		name = self._settled_request()
		refund = {
			"id": 901,
			"uuid": "txn-refund-1",
			"status": "refunded",
			"amount": 10000,
			"invoice": {"currency": "CHF"},
		}
		for _attempt in range(3):
			self._deliver(name, refund)

		self.assertEqual(len(_reversals(name)), 1)
		self.assertEqual(len(_todos(name, ps_module.REFUND_TODO_MARKER)), 1)

	def test_two_partial_refunds_are_both_recorded(self):
		name = self._settled_request()
		for uuid, amount in (("txn-refund-a", 4000), ("txn-refund-b", 6000)):
			self._deliver(
				name,
				{
					"uuid": uuid,
					"status": "partially-refunded",
					"amount": amount,
					"invoice": {"currency": "CHF"},
				},
			)

		self.assertEqual([entry["amount"] for entry in _reversals(name)], [4000, 6000])
		self.assertEqual(len(_todos(name, ps_module.REFUND_TODO_MARKER)), 2)

	def test_pending_refund_is_recorded_without_raising_a_todo(self):
		name = self._settled_request()
		self._deliver(name, {"uuid": "txn-refund-p", "status": "refund_pending", "amount": 10000})

		self.assertEqual(self.assertOneReversal(name)["status"], "refund_pending")
		# Nothing has moved yet — a ToDo now would be noise.
		self.assertEqual(_todos(name, ps_module.REFUND_TODO_MARKER), [])

	def test_pending_refund_progresses_to_final_with_the_same_provider_event(self):
		name = self._settled_request()
		self._deliver(
			name,
			{
				"uuid": "txn-refund-progress",
				"status": "refund_pending",
				"amount": 10000,
				"invoice": {"currency": "CHF"},
			},
		)
		self._deliver(
			name,
			{
				"uuid": "txn-refund-progress",
				"status": "refunded",
				"amount": 10000,
				"invoice": {"currency": "CHF"},
			},
		)
		# A provider status transition updates one event; it is not mistaken for
		# either a replay or a second refund.
		self.assertEqual([entry["status"] for entry in _reversals(name)], ["refunded"])
		self.assertEqual(len(_todos(name, ps_module.REFUND_TODO_MARKER)), 1)

		self._deliver(
			name,
			{
				"uuid": "txn-refund-progress",
				"status": "refunded",
				"amount": 10000,
				"invoice": {"currency": "CHF"},
			},
		)
		self.assertEqual(len(_reversals(name)), 1)
		self.assertEqual(len(_todos(name, ps_module.REFUND_TODO_MARKER)), 1)

	def test_dispute_is_recorded_and_does_not_block_a_later_chargeback(self):
		name = self._settled_request()
		self._deliver(name, {"uuid": "txn-dispute", "status": "disputed", "amount": 10000})

		self.assertEqual(self.assertOneReversal(name)["status"], "disputed")
		self.assertEqual(len(_todos(name, ps_module.DISPUTE_TODO_MARKER)), 1)

		self._deliver(name, {"uuid": "txn-chargeback", "status": "chargeback", "amount": 10000})
		self.assertEqual(frappe.db.get_value("Integration Request", name, "status"), "Failed")

	def test_refund_after_a_chargeback_is_ignored(self):
		"""Chargeback is terminal: it already demands a manual reversal."""
		name = self._settled_request()
		self._deliver(name, {"uuid": "txn-chargeback", "status": "chargeback", "amount": 10000})
		self._deliver(name, {"uuid": "txn-refund-late", "status": "refunded", "amount": 10000})

		self.assertEqual(_reversals(name), [])
		self.assertEqual(frappe.db.get_value("Integration Request", name, "status"), "Failed")

	def test_unrelated_delayed_status_still_cannot_touch_a_completed_request(self):
		name = self._settled_request()
		for status in ("waiting", "declined", "confirmed"):
			self._deliver(name, {"uuid": f"txn-{status}", "status": status, "amount": 10000})

		self.assertEqual(_reversals(name), [])
		self.assertEqual(frappe.db.get_value("Integration Request", name, "status"), "Completed")
		data = frappe.parse_json(frappe.db.get_value("Integration Request", name, "data"))
		self.assertEqual(data["payrexx_transaction"]["uuid"], "txn-confirmed")

	def assertOneReversal(self, integration_request_name: str) -> dict:
		reversals = _reversals(integration_request_name)
		self.assertEqual(len(reversals), 1)
		return reversals[0]


class TestReversalKeying(IntegrationTestCase):
	def test_event_key_prefers_uuid_then_id_then_payload_digest(self):
		self.assertEqual(ps_module._provider_event_key({"uuid": "u", "id": 5}), "u")
		self.assertEqual(ps_module._provider_event_key({"id": 5}), "5")
		digest = ps_module._provider_event_key({"status": "refunded", "amount": 100})
		self.assertEqual(len(digest), 32)
		# Same body, same event — key order must not change the identity.
		self.assertEqual(digest, ps_module._provider_event_key({"amount": 100, "status": "refunded"}))


class TestReversalAmountLabel(IntegrationTestCase):
	def test_amount_is_rendered_from_the_smallest_currency_unit(self):
		self.assertEqual(ps_module._reversal_amount_label({"amount": 10000, "currency": "CHF"}), "CHF 100.00")
		self.assertEqual(ps_module._reversal_amount_label({"amount": 550, "currency": "EUR"}), "EUR 5.50")
		self.assertEqual(ps_module._reversal_amount_label({"amount": None, "status": "refunded"}), "refunded")


assert GATEWAY_NAME  # imported for the shared settings fixture name
