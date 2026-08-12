# Copyright (c) 2026, Goodvantage GmbH and contributors

"""Payrexx subscriptions: client surface, gateway signup, and webhook routing.

The load-bearing behaviour here is the split in `_process_subscription_charge`.
Payrexx echoes the same `referenceId` on every charge of a subscription, so a
donor's twelfth payment arrives pointing at the Integration Request that settled
their first one. Getting that wrong silently discards real money.
"""

from __future__ import annotations

import hashlib
import hmac
from contextlib import nullcontext
from datetime import datetime
from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from requests import HTTPError, Response

from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
	payrexx_settings as ps_module,
)
from payrexx_integration.payrexx_integration.doctype.payrexx_settings.test_payrexx_settings import (
	_create_test_user,
	_ensure_settings,
)
from payrexx_integration.payrexx_integration.payrexx import webhook_payload
from payrexx_integration.payrexx_integration.payrexx.payrexx_client import (
	PayrexxAPIError,
	PayrexxClient,
	validate_subscription_interval,
)

_CLIENT_MODULE = "payrexx_integration.payrexx_integration.payrexx.payrexx_client"
_SUCCESS = {"status": "success", "data": [{"id": 42, "status": "active"}]}


class TestSubscriptionInterval(UnitTestCase):
	def test_supported_intervals_are_normalised(self):
		self.assertEqual(validate_subscription_interval("p1m", "i"), "P1M")
		self.assertEqual(validate_subscription_interval(" P3M ", "i"), "P3M")
		self.assertEqual(validate_subscription_interval("P1Y", "i"), "P1Y")

	def test_anything_else_is_refused(self):
		for value in ("", None, "P1W", "P10D", "1M", "P0", "PXM", "P1M; DROP", "P1000M"):
			with self.subTest(value=value), self.assertRaises(ValueError):
				validate_subscription_interval(value, "subscription_interval")


class TestSubscriptionClient(UnitTestCase):
	def _client(self) -> PayrexxClient:
		return PayrexxClient(instance="demo", api_secret="sk_test_dummy")

	def test_crud_hits_the_documented_routes(self):
		client = self._client()
		with patch(f"{_CLIENT_MODULE}._execute_request", return_value=_SUCCESS) as execute:
			client.retrieve_subscription(42)
			client.update_subscription(42, {"amount": 5000})
			client.cancel_subscription(42)
			client.create_subscription({"userId": 1, "psp": 4})

		calls = [(call.args[0], call.args[1]) for call in execute.call_args_list]
		self.assertEqual([method for method, _url in calls], ["GET", "PUT", "DELETE", "POST"])
		self.assertTrue(all("/Subscription/" in url for _method, url in calls))
		self.assertIn("/Subscription/42/?instance=demo", calls[0][1])

	def test_list_returns_every_row_not_just_the_first(self):
		client = self._client()
		page = {"status": "success", "data": [{"id": 1}, {"id": 2}, {"id": 3}]}
		with patch(f"{_CLIENT_MODULE}._execute_request", return_value=page) as execute:
			rows = client.list_subscriptions(offset=100, limit=50)

		self.assertEqual([row["id"] for row in rows], [1, 2, 3])
		self.assertIn("offset=100", execute.call_args.args[1])
		self.assertIn("limit=50", execute.call_args.args[1])
		self.assertIsNone(execute.call_args.kwargs.get("json_data"))

	def test_transaction_list_uses_the_official_sdk_query_parameters(self):
		client = self._client()
		with patch(f"{_CLIENT_MODULE}._execute_request", return_value=_SUCCESS) as execute:
			client.list_transactions(
				datetime_utc_greater_than="2026-08-01 00:00:00",
				datetime_utc_less_than="2026-08-08 00:00:00",
				my_transactions_only=True,
				order_by_time="ASC",
				offset=100,
				limit=50,
			)

		url = execute.call_args.args[1]
		for expected in (
			"filterDatetimeUtcGreaterThan=2026-08-01+00%3A00%3A00",
			"filterDatetimeUtcLessThan=2026-08-08+00%3A00%3A00",
			"filterMyTransactionsOnly=1",
			"orderByTime=ASC",
			"offset=100",
			"limit=50",
		):
			self.assertIn(expected, url)
		self.assertIsNone(execute.call_args.kwargs.get("json_data"))

	def test_cancel_rejects_provider_error_envelope(self):
		client = self._client()
		response = {"status": "error", "message": "Subscription is still active"}
		with (
			patch(f"{_CLIENT_MODULE}._execute_request", return_value=response),
			patch(f"{_CLIENT_MODULE}.log_sanitized_error") as log_error,
			self.assertRaises(PayrexxAPIError),
		):
			client.cancel_subscription(42, expected_statuses=(404,))
		log_error.assert_called_once()
		self.assertEqual(log_error.call_args.args[0], "payrexx_response")

	def test_cancel_treats_verified_provider_404_as_idempotent_success(self):
		client = self._client()
		response = Response()
		response.status_code = 404
		with patch(f"{_CLIENT_MODULE}._execute_request", side_effect=HTTPError(response=response)) as execute:
			result = client.cancel_subscription(42, expected_statuses=(404,))

		self.assertEqual(result, {"status": "success", "already_gone": True})
		execute.assert_called_once()

	def test_cancel_accepts_an_empty_success_response(self):
		client = self._client()
		with patch(f"{_CLIENT_MODULE}._execute_request", return_value=None):
			self.assertEqual(client.cancel_subscription(42), {"status": "success"})

	def test_subscription_create_404_never_replays_on_the_default_host(self):
		response = Response()
		response.status_code = 404
		with (
			patch(f"{_CLIENT_MODULE}.frappe.conf", {"payrexx_allowed_api_hosts": ["api.pay.example"]}),
			patch(f"{_CLIENT_MODULE}._execute_request", side_effect=HTTPError(response=response)) as execute,
		):
			client = PayrexxClient(
				instance="demo",
				api_secret="sk_test_dummy",
				api_base_domain="pay.example",
			)
			with self.assertRaises(HTTPError):
				client.create_subscription({"userId": 1})

		execute.assert_called_once()
		self.assertIn("api.pay.example", execute.call_args.args[1])


class TestSubscriptionGatewayPayload(IntegrationTestCase):
	def _payload(self, **kwargs) -> dict:
		settings = frappe.get_doc("Payrexx Settings", _ensure_settings())
		settings.enable_managed_subscriptions = 1
		values = {
			"amount": 50,
			"currency": "CHF",
			"description": "Monthly gift",
			"payer_email": "donor@example.test",
			"payer_name": "Anna Muster",
			**kwargs,
		}
		with patch(
			"payrexx_integration.payrexx_integration.doctype.payrexx_settings."
			"payrexx_settings._canonical_gateway_amount",
			return_value=5000,
		):
			return settings._build_create_gateway_payload(values, "IR-TEST")

	def test_signup_sends_the_subscription_parameters(self):
		payload = self._payload(
			subscription_state=True,
			subscription_interval="P1M",
			subscription_period="P1Y",
			subscription_cancellation_interval="P1M",
		)
		self.assertTrue(payload["subscriptionState"])
		self.assertEqual(payload["subscriptionInterval"], "P1M")
		self.assertEqual(payload["subscriptionPeriod"], "P1Y")
		self.assertEqual(payload["subscriptionCancellationInterval"], "P1M")

	def test_optional_parameters_are_omitted_rather_than_guessed(self):
		payload = self._payload(subscription_state=True, subscription_interval="P1M")
		self.assertNotIn("subscriptionPeriod", payload)
		self.assertNotIn("subscriptionCancellationInterval", payload)

	def test_a_one_off_checkout_is_untouched(self):
		payload = self._payload()
		self.assertNotIn("subscriptionState", payload)

	def test_a_bad_interval_fails_the_checkout_instead_of_degrading_it(self):
		"""Silently dropping this would bill the donor on the wrong cadence for years."""
		with self.assertRaises(frappe.ValidationError):
			self._payload(subscription_state=True, subscription_interval="P1W")
		with self.assertRaises(frappe.ValidationError):
			self._payload(subscription_state=True, subscription_interval="")

	def test_subscription_signup_is_disabled_by_default(self):
		settings = frappe.get_doc("Payrexx Settings", _ensure_settings())
		settings.enable_managed_subscriptions = 0
		with self.assertRaisesRegex(frappe.ValidationError, "Managed subscriptions are disabled"):
			settings._build_create_gateway_payload(
				{
					"amount": 50,
					"currency": "CHF",
					"subscription_state": True,
					"subscription_interval": "P1M",
				},
				"IR-TEST",
			)


class TestSubscriptionWebhookRouting(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.settings_name = _ensure_settings()
		frappe.db.set_value(
			"Payrexx Settings",
			self.settings_name,
			"allow_test_transactions",
			0,
			update_modified=False,
		)
		frappe.clear_document_cache("Payrexx Settings", self.settings_name)
		self.claimed: list[dict] = []

	def _claiming_provider(self):
		def provider(**context):
			self.claimed.append(context)
			return True

		return patch.object(
			ps_module,
			"_dispatch_subscription_event",
			side_effect=lambda event, **c: bool(provider(event=event, **c)),
		)

	def _request(self, status: str, data: dict | None = None) -> str:
		return (
			frappe.get_doc(
				{
					"doctype": "Integration Request",
					"integration_request_service": "Payrexx",
					"status": status,
					"data": frappe.as_json({"payrexx_settings": self.settings_name, **(data or {})}),
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	@staticmethod
	def _charge(reference: str, *, uuid: str, subscription_status: str = "active") -> dict:
		return {
			"uuid": uuid,
			"id": 500,
			"status": "confirmed",
			"mode": "LIVE",
			"amount": 5000,
			"invoice": {"referenceId": reference, "currency": "CHF"},
			"subscription": {
				"id": 42,
				"status": subscription_status,
				"valid_until": "2026-09-07",
				"paymentInterval": "P1M",
			},
		}

	def test_signup_charge_settles_through_the_ordinary_path(self):
		"""The first charge must not get its own settlement implementation."""
		name = self._request("Queued")
		with patch.object(ps_module, "_dispatch_subscription_event") as dispatch:
			result = ps_module._process_subscription_charge(
				self.settings_name,
				self._charge(name, uuid="txn-signup"),
				{"id": 42},
				name,
				"confirmed",
				integration_request=frappe.get_doc("Integration Request", name),
			)
		self.assertIsNone(result, "an unsettled request must fall through to normal settlement")
		dispatch.assert_not_called()

	def test_later_installment_is_dispatched_not_discarded(self):
		name = self._request("Completed", {"payrexx_transaction": {"uuid": "txn-signup"}})
		with self._claiming_provider():
			result = ps_module._process_subscription_charge(
				self.settings_name,
				self._charge(name, uuid="txn-installment-2"),
				{"id": 42},
				name,
				"confirmed",
				integration_request=frappe.get_doc("Integration Request", name),
			)

		self.assertEqual(result, {"ok": True})
		self.assertEqual(len(self.claimed), 1)
		self.assertEqual(self.claimed[0]["event"], "charge")
		self.assertEqual(self.claimed[0]["reference_id"], name)
		self.assertEqual(
			frappe.db.get_value(
				ps_module.SUBSCRIPTION_EVENT_DOCTYPE,
				{"provider_event_id": "txn-installment-2"},
				"dispatch_status",
			),
			"Claimed",
		)

	def test_replayed_installment_is_claimed_durably_before_provider_dispatch(self):
		name = self._request("Completed", {"payrexx_transaction": {"uuid": "txn-signup"}})
		charge = self._charge(name, uuid="txn-installment-replayed")
		integration_request = frappe.get_doc("Integration Request", name)
		with self._claiming_provider():
			for _attempt in range(3):
				self.assertEqual(
					ps_module._process_subscription_charge(
						self.settings_name,
						charge,
						charge["subscription"],
						name,
						"confirmed",
						integration_request=integration_request,
					),
					{"ok": True},
				)

		self.assertEqual(len(self.claimed), 1)
		self.assertEqual(
			frappe.db.count(
				ps_module.SUBSCRIPTION_EVENT_DOCTYPE,
				{"provider_event_id": "txn-installment-replayed"},
			),
			1,
		)

	def test_waiting_and_authorized_do_not_consume_later_confirmation(self):
		name = self._request("Completed", {"payrexx_transaction": {"uuid": "txn-signup"}})
		waiting = self._charge(name, uuid="txn-progress") | {"status": "waiting"}
		authorized = self._charge(name, uuid="txn-progress") | {"status": "authorized"}
		confirmed = self._charge(name, uuid="txn-progress")
		integration_request = frappe.get_doc("Integration Request", name)

		with self._claiming_provider():
			ps_module._process_subscription_charge(
				self.settings_name,
				waiting,
				waiting["subscription"],
				name,
				"waiting",
				integration_request=integration_request,
			)
			ps_module._process_subscription_charge(
				self.settings_name,
				authorized,
				authorized["subscription"],
				name,
				"authorized",
				integration_request=integration_request,
			)
			ps_module._process_subscription_charge(
				self.settings_name,
				confirmed,
				confirmed["subscription"],
				name,
				"confirmed",
				integration_request=integration_request,
			)

		self.assertEqual([event["status"] for event in self.claimed], ["waiting", "authorized", "confirmed"])
		self.assertEqual(
			frappe.db.get_value(
				ps_module.SUBSCRIPTION_EVENT_DOCTYPE,
				{"provider_event_id": "txn-progress"},
				["provider_status", "dispatch_status"],
			),
			("confirmed", "Claimed"),
		)

	def test_documented_uncaptured_installment_is_dispatched(self):
		name = self._request("Completed", {"payrexx_transaction": {"uuid": "txn-signup"}})
		charge = self._charge(name, uuid="txn-uncaptured") | {"status": "uncaptured"}
		with self._claiming_provider():
			self.assertEqual(
				ps_module._process_subscription_charge(
					self.settings_name,
					charge,
					charge["subscription"],
					name,
					"uncaptured",
					integration_request=frappe.get_doc("Integration Request", name),
				),
				{"ok": True},
			)

		self.assertEqual(self.claimed[0]["status"], "uncaptured")
		self.assertEqual(
			frappe.db.get_value(
				ps_module.SUBSCRIPTION_EVENT_DOCTYPE,
				{"provider_event_id": "txn-uncaptured"},
				"provider_status",
			),
			"uncaptured",
		)

	def test_unclaimed_status_can_be_retried_after_provider_repair(self):
		name = self._request("Completed", {"payrexx_transaction": {"uuid": "txn-signup"}})
		charge = self._charge(name, uuid="txn-unclaimed-retry")
		integration_request = frappe.get_doc("Integration Request", name)

		with patch.object(ps_module, "_dispatch_subscription_event", return_value=False):
			self.assertEqual(
				ps_module._process_subscription_charge(
					self.settings_name,
					charge,
					charge["subscription"],
					name,
					"confirmed",
					integration_request=integration_request,
				),
				{"ok": False, "error": "subscription_event_unclaimed"},
			)
		self.assertEqual(
			frappe.db.get_value(
				ps_module.SUBSCRIPTION_EVENT_DOCTYPE,
				{"provider_event_id": "txn-unclaimed-retry"},
				"dispatch_status",
			),
			"Unclaimed",
		)

		with self._claiming_provider():
			self.assertEqual(
				ps_module._process_subscription_charge(
					self.settings_name,
					charge,
					charge["subscription"],
					name,
					"confirmed",
					integration_request=integration_request,
				),
				{"ok": True},
			)
		self.assertEqual(len(self.claimed), 1)

	def test_subscription_reversal_uses_the_reversal_state_machine(self):
		name = self._request("Completed", {"payrexx_transaction": {"uuid": "txn-signup"}})
		refund = self._charge(name, uuid="txn-refund") | {
			"status": "refunded",
			"originalTransactionUuid": "txn-signup",
		}
		with (
			patch.object(ps_module, "_process_subscription_charge") as process_charge,
			patch.object(ps_module, "_record_reversal_evidence") as record_reversal,
		):
			self.assertEqual(
				ps_module._process_callback_transaction(self.settings_name, refund, name, "refunded"),
				{"ok": True},
			)
		process_charge.assert_not_called()
		record_reversal.assert_called_once()

	def test_installment_reversals_are_independent_of_the_shared_signup_request(self):
		name = self._request("Completed", {"payrexx_transaction": {"uuid": "txn-signup"}})
		chargeback = self._charge(name, uuid="reversal-chargeback") | {
			"status": "chargeback",
			"originalTransactionUuid": "txn-installment-2",
		}
		refund = self._charge(name, uuid="reversal-refund") | {
			"status": "refunded",
			"originalTransactionUuid": "txn-installment-3",
		}

		with self._claiming_provider():
			self.assertEqual(
				ps_module._process_callback_transaction(self.settings_name, chargeback, name, "chargeback"),
				{"ok": True},
			)
			self.assertEqual(
				ps_module._process_callback_transaction(self.settings_name, refund, name, "refunded"),
				{"ok": True},
			)

		request = frappe.get_doc("Integration Request", name)
		self.assertEqual(request.status, "Completed")
		self.assertEqual((frappe.parse_json(request.data) or {})["payrexx_transaction"]["uuid"], "txn-signup")
		self.assertEqual(
			frappe.db.count(
				ps_module.SUBSCRIPTION_EVENT_DOCTYPE,
				{"event_type": "Reversal", "dispatch_status": "Claimed"},
			),
			2,
		)

	def test_replayed_signup_charge_is_not_recorded_twice(self):
		"""The boundary between 'settled already' and 'new installment'."""
		name = self._request("Completed", {"payrexx_transaction": {"uuid": "txn-signup"}})
		with patch.object(ps_module, "_dispatch_subscription_event") as dispatch:
			result = ps_module._process_subscription_charge(
				self.settings_name,
				self._charge(name, uuid="txn-signup"),
				{"id": 42},
				name,
				"confirmed",
				integration_request=frappe.get_doc("Integration Request", name),
			)

		self.assertEqual(result, {"ok": True})
		dispatch.assert_not_called()

	def test_installment_with_no_surviving_request_is_still_dispatched(self):
		with self._claiming_provider():
			result = ps_module._process_subscription_charge(
				self.settings_name,
				self._charge("IR-GONE", uuid="txn-installment"),
				{"id": 42},
				"IR-GONE",
				"confirmed",
				integration_request=None,
			)
		self.assertEqual(result, {"ok": True})
		self.assertEqual(len(self.claimed), 1)

	def test_test_mode_installment_is_refused(self):
		name = self._request("Completed", {"payrexx_transaction": {"uuid": "txn-signup"}})
		charge = self._charge(name, uuid="txn-test") | {"mode": "TEST"}
		with patch.object(ps_module, "_dispatch_subscription_event") as dispatch:
			result = ps_module._process_subscription_charge(
				self.settings_name,
				charge,
				{"id": 42},
				name,
				"confirmed",
				integration_request=frappe.get_doc("Integration Request", name),
			)
		self.assertEqual(result, {"ok": True})
		dispatch.assert_not_called()

	def test_a_one_off_transaction_never_enters_the_subscription_path(self):
		name = self._request("Queued")
		transaction = {"uuid": "txn-oneoff", "status": "confirmed", "invoice": {"referenceId": name}}
		self.assertEqual(webhook_payload.embedded_subscription(transaction), {})

	def test_callback_classifies_after_the_current_request_lock(self):
		"""The locked row, not a preliminary scalar snapshot, decides installment routing."""
		locked_request = frappe._dict(
			name="IR-RACE",
			integration_request_service="Payrexx",
			status="Completed",
			data=frappe.as_json(
				{"payrexx_settings": self.settings_name, "payrexx_transaction": {"uuid": "signup"}}
			),
		)
		charge = self._charge("IR-RACE", uuid="installment-after-concurrent-signup")
		order: list[str] = []

		def lock_request(*_args, **_kwargs):
			order.append("lock")
			return locked_request

		def classify(*_args, **kwargs):
			order.append("classify")
			self.assertIs(kwargs["integration_request"], locked_request)
			return {"ok": True}

		with (
			patch.object(ps_module, "_get_current_locked_doc", side_effect=lock_request),
			patch.object(ps_module, "_process_subscription_charge", side_effect=classify),
		):
			self.assertEqual(
				ps_module._process_callback_transaction(self.settings_name, charge, "IR-RACE", "confirmed"),
				{"ok": True},
			)
		self.assertEqual(order, ["lock", "classify"])

	def test_lifecycle_and_installment_hooks_run_as_the_gateway_automation_user(self):
		automation_user = _create_test_user()
		settings_name = _ensure_settings(
			f"Subscription-{frappe.generate_hash(length=8)}", automation_user=automation_user
		)
		observed_users: list[str] = []

		with patch.object(
			ps_module,
			"_dispatch_subscription_event",
			side_effect=lambda *_args, **_kwargs: observed_users.append(frappe.session.user) or True,
		):
			ps_module._process_callback_subscription(settings_name, {"id": 42, "status": "active"})
			ps_module._process_subscription_charge(
				settings_name,
				self._charge("IR-GONE-AUTO", uuid="txn-auto-user"),
				{"id": 42},
				"IR-GONE-AUTO",
				"confirmed",
				integration_request=None,
			)

		self.assertEqual(observed_users, [automation_user, automation_user])

	def test_lifecycle_webhook_queues_scoped_transaction_recovery_after_commit(self):
		subscription = self._charge("IR-123", uuid="unused")["subscription"] | {
			"invoice": {"referenceId": "IR-123"}
		}
		with (
			patch.object(ps_module, "_dispatch_subscription_event", return_value=True),
			patch.object(ps_module.frappe, "enqueue") as enqueue,
		):
			self.assertEqual(
				ps_module._process_callback_subscription(self.settings_name, subscription),
				{"ok": True},
			)

		self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])
		self.assertTrue(enqueue.call_args.kwargs["deduplicate"])
		self.assertEqual(enqueue.call_args.kwargs["gateway_name"], self.settings_name)
		self.assertEqual(enqueue.call_args.kwargs["subscription_id"], "42")
		self.assertEqual(enqueue.call_args.kwargs["reference_id"], "IR-123")


class TestSubscriptionReconciliation(IntegrationTestCase):
	def test_missed_confirmed_installment_is_recovered_from_transaction_list(self):
		settings = Mock()
		settings.name = "Live"
		transaction = {
			"uuid": "txn-missed-installment",
			"status": "confirmed",
			"invoice": {"referenceId": "IR-SIGNUP", "currency": "CHF"},
			"subscription": {"id": 42, "status": "active", "paymentInterval": "P1M"},
		}
		settings._client.return_value.list_transactions.return_value = [transaction]
		with patch.object(ps_module, "_process_callback_transaction", return_value={"ok": True}) as process:
			result = ps_module._reconcile_settings_transactions(
				settings,
				window_start="2026-08-01 00:00:00",
				window_end="2026-08-08 00:00:00",
			)

		self.assertEqual(result, {"seen": 1, "processed": 1, "failed": 0})
		process.assert_called_once_with("Live", transaction, "IR-SIGNUP", "confirmed")

	def test_successful_transaction_window_advances_the_utc_cursor_with_overlap(self):
		settings = frappe._dict(
			name="Live",
			transaction_reconciliation_cursor="2026-08-07 00:00:00",
		)
		settings.db_set = Mock()
		with (
			patch.object(ps_module, "_utc_now", return_value=datetime(2026, 8, 8, 0, 0, 0)),
			patch.object(
				ps_module,
				"_reconcile_settings_transactions",
				return_value={"seen": 1, "processed": 1, "failed": 0},
			) as reconcile,
		):
			result = ps_module._reconcile_settings_transactions_with_cursor(settings, commit_each=False)

		self.assertEqual(result, {"seen": 1, "processed": 1, "failed": 0})
		self.assertEqual(reconcile.call_args.kwargs["window_start"], datetime(2026, 8, 6, 18, 0, 0))
		self.assertEqual(reconcile.call_args.kwargs["window_end"], datetime(2026, 8, 8, 0, 0, 0))
		settings.db_set.assert_called_once_with(
			"transaction_reconciliation_cursor",
			datetime(2026, 8, 8, 0, 0, 0),
			update_modified=False,
		)

	def test_failed_transaction_window_does_not_advance_the_cursor(self):
		settings = frappe._dict(name="Live", transaction_reconciliation_cursor="")
		settings.db_set = Mock()
		with patch.object(
			ps_module,
			"_reconcile_settings_transactions",
			return_value={"seen": 1, "processed": 0, "failed": 1},
		):
			ps_module._reconcile_settings_transactions_with_cursor(settings, commit_each=False)

		settings.db_set.assert_not_called()

	def test_sweep_pages_and_reports_without_settling(self):
		settings = Mock()
		settings.name = "TestGW"
		client = settings._client.return_value
		client.list_subscriptions.side_effect = [
			[{"id": n} for n in range(100)],
			[{"id": 100}],
		]
		with (
			patch.object(ps_module, "_resolve_settings", return_value=settings),
			patch.object(ps_module, "as_automation_user", return_value=nullcontext()) as automation,
			patch.object(
				ps_module,
				"_reconcile_settings_transactions_with_cursor",
				return_value={"seen": 0, "processed": 0, "failed": 0},
			),
			patch.object(ps_module, "_dispatch_subscription_event", return_value=True) as dispatch,
		):
			result = ps_module.reconcile_subscriptions("TestGW")

		self.assertEqual(result, {"subscriptions": 101, "claimed": 101, "failed": 0})
		self.assertEqual(
			[call.kwargs["offset"] for call in client.list_subscriptions.call_args_list], [0, 100]
		)
		# Reporting only: the sweep never dispatches a "charge", because a list
		# endpoint carries no transaction to settle.
		self.assertEqual({call.args[0] for call in dispatch.call_args_list}, {"status"})
		self.assertEqual(automation.call_count, 2)

	def test_nothing_claimed_is_reported_as_a_misconfiguration(self):
		settings = Mock()
		settings.name = "TestGW"
		settings._client.return_value.list_subscriptions.side_effect = [[{"id": 1}]]
		with (
			patch.object(ps_module, "_resolve_settings", return_value=settings),
			patch.object(ps_module, "as_automation_user", return_value=nullcontext()),
			patch.object(
				ps_module,
				"_reconcile_settings_transactions_with_cursor",
				return_value={"seen": 0, "processed": 0, "failed": 0},
			),
			patch.object(ps_module, "_dispatch_subscription_event", return_value=False),
			patch.object(ps_module.frappe, "log_error") as log_error,
		):
			result = ps_module.reconcile_subscriptions("TestGW")

		self.assertEqual(result, {"subscriptions": 1, "claimed": 0, "failed": 0})
		log_error.assert_called_once()

	def test_scheduler_queues_one_deduplicated_job_per_gateway(self):
		with (
			patch.object(ps_module.frappe, "get_all", return_value=["Live", "Sandbox"]),
			patch.object(ps_module.frappe, "enqueue") as enqueue,
		):
			result = ps_module.enqueue_subscription_reconciliation()

		self.assertEqual(result, {"gateways": 2})
		self.assertEqual(
			[call.kwargs["gateway_name"] for call in enqueue.call_args_list], ["Live", "Sandbox"]
		)
		self.assertTrue(all(call.kwargs["deduplicate"] for call in enqueue.call_args_list))

	def test_one_subscription_failure_does_not_starve_later_rows(self):
		settings = Mock()
		settings.name = "TestGW"
		settings._client.return_value.list_subscriptions.return_value = [{"id": 1}, {"id": 2}, {"id": 3}]
		with (
			patch.object(ps_module, "as_automation_user", return_value=nullcontext()),
			patch.object(
				ps_module,
				"_dispatch_subscription_event",
				side_effect=[True, RuntimeError("one bad subscription"), True],
			) as dispatch,
			patch.object(ps_module.frappe.db, "commit") as commit,
			patch.object(ps_module.frappe.db, "rollback") as rollback,
			patch.object(ps_module.frappe, "log_error"),
		):
			result = ps_module._reconcile_settings_subscriptions(settings, commit_each=True)

		self.assertEqual(result, {"subscriptions": 3, "claimed": 2, "failed": 1})
		self.assertEqual(dispatch.call_count, 3)
		self.assertEqual(commit.call_count, 3)
		rollback.assert_called_once_with()


class TestWebhookPayloadShape(UnitTestCase):
	def test_a_delivery_is_a_transaction_or_a_subscription_never_both(self):
		self.assertTrue(webhook_payload.is_subscription_event({"subscription": {"id": 1}}))
		self.assertFalse(
			webhook_payload.is_subscription_event({"transaction": {"id": 1, "subscription": {"id": 2}}})
		)
		self.assertFalse(webhook_payload.is_subscription_event({"transaction": {"id": 1}}))

	def test_documented_bare_subscription_is_a_lifecycle_event(self):
		bare = {
			"id": 42,
			"status": "active",
			"valid_until": "2026-09-07",
			"paymentInterval": "P1M",
		}
		self.assertTrue(webhook_payload.is_subscription_event(bare))
		self.assertIs(webhook_payload.subscription_of(bare), bare)

	def test_arbitrary_bare_json_is_not_guessed_as_a_subscription(self):
		self.assertFalse(webhook_payload.is_subscription_event({"id": 42, "status": "active"}))

	def test_reference_prefers_the_invoice_then_the_transaction(self):
		self.assertEqual(
			webhook_payload.reference_id({"invoice": {"referenceId": "A"}, "referenceId": "B"}), "A"
		)
		self.assertEqual(webhook_payload.reference_id({"referenceId": "B"}), "B")
		self.assertEqual(webhook_payload.reference_id({}), "")

	def test_live_mode_is_undecidable_without_either_marker(self):
		self.assertTrue(webhook_payload.is_live({"mode": "LIVE"}))
		self.assertFalse(webhook_payload.is_live({"mode": "TEST"}))
		self.assertFalse(webhook_payload.is_live({"invoice": {"test": 1}}))
		# Neither marker: treated as live, matching the settlement gate's rule
		# that an undecidable transaction keeps pre-existing behaviour.
		self.assertTrue(webhook_payload.is_live({}))

	def test_every_field_is_optional(self):
		for reader in (
			webhook_payload.subscription_id,
			webhook_payload.subscription_status,
			webhook_payload.subscription_next_payment,
			webhook_payload.subscription_interval,
		):
			with self.subTest(reader=reader.__name__):
				self.assertEqual(reader({}), "")
				self.assertEqual(reader(None), "")

	def test_digest_identified_event_can_redrive_from_its_sanitized_payload(self):
		transaction = {
			"status": "confirmed",
			"amount": 5000,
			"time": "2026-08-07T08:00:00+00:00",
			"invoice": {"currency": "CHF"},
			"payer": {"email": "not-persisted@example.test"},
		}
		subscription = {"id": 42, "status": "active", "paymentInterval": "P1M"}
		event = frappe._dict(
			name="EVENT-DIGEST",
			dispatch_status="Unclaimed",
			payrexx_settings="Live",
			subscription_id="42",
			provider_event_id=ps_module._provider_event_key(transaction),
			provider_status="confirmed",
			reference_id="IR-GONE",
			redrive_payload=frappe.as_json(
				ps_module._subscription_redrive_payload(subscription, transaction)
			),
			db_set=Mock(),
		)
		with (
			patch.object(ps_module, "_get_current_locked_doc", return_value=event),
			patch.object(ps_module, "_dispatch_subscription_event", return_value=True) as dispatch,
		):
			self.assertTrue(ps_module._redrive_subscription_event(event.name, "Live"))

		dispatch.assert_called_once()


class TestDocumentedSubscriptionCallbackShape(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.settings_name = _ensure_settings()

	def _deliver(self, payload: dict):
		body = frappe.as_json(payload).encode()
		signing_key = frappe.get_doc("Payrexx Settings", self.settings_name).get_password(
			"webhook_signing_key"
		)
		signature = hmac.new(signing_key.encode(), body, hashlib.sha256).hexdigest()

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

	def test_documented_bare_lifecycle_payload_is_dispatched(self):
		payload = {
			"id": 42,
			"status": "active",
			"valid_until": "2026-09-07",
			"paymentInterval": "P1M",
		}
		with patch.object(ps_module, "_dispatch_subscription_event", return_value=True) as dispatch:
			self.assertEqual(self._deliver(payload), {"ok": True})
		dispatch.assert_called_once_with("status", subscription=payload, settings_name=self.settings_name)

	def test_unknown_authenticated_shape_is_not_acknowledged(self):
		with self.assertRaisesRegex(frappe.ValidationError, "unsupported JSON webhook shape"):
			self._deliver({"id": 42, "status": "active"})

	def test_unclaimed_financial_event_without_reference_is_not_acknowledged(self):
		payload = {
			"transaction": {
				"uuid": "txn-no-reference",
				"status": "confirmed",
				"mode": "LIVE",
				"amount": 5000,
				"invoice": {"currency": "CHF"},
				"subscription": {"id": 42, "status": "active", "paymentInterval": "P1M"},
			}
		}
		with patch.object(ps_module, "_dispatch_subscription_event", return_value=False):
			self.assertEqual(self._deliver(payload), {"ok": False, "error": "subscription_event_unclaimed"})
		self.assertEqual(frappe.local.response.get("http_status_code"), 503)
		self.assertEqual(
			frappe.db.get_value(
				ps_module.SUBSCRIPTION_EVENT_DOCTYPE,
				{"provider_event_id": "txn-no-reference"},
				"dispatch_status",
			),
			"Unclaimed",
		)

	def test_provider_failure_is_durable_and_daily_redrive_can_claim_the_charge(self):
		# The callback rolls back the complete request transaction before recording
		# recovery evidence; mirror production by making its settings row durable.
		frappe.db.commit()
		partial_todo = f"Partial provider effect {frappe.generate_hash(length=8)}"
		payload = {
			"transaction": {
				"uuid": "txn-provider-failure",
				"status": "confirmed",
				"mode": "LIVE",
				"time": "2026-08-07T08:00:00+00:00",
				"amount": 5000,
				"payer": {"email": "must-not-be-persisted@example.test"},
				"invoice": {"currency": "CHF"},
				"subscription": {
					"id": 42,
					"status": "active",
					"paymentInterval": "P1M",
					"contact": {"email": "also-private@example.test"},
				},
			}
		}

		def fail_after_partial_write(*_args, **_kwargs):
			frappe.get_doc(
				{
					"doctype": "ToDo",
					"allocated_to": "Administrator",
					"description": partial_todo,
				}
			).insert(ignore_permissions=True)
			raise RuntimeError("broken")

		with (
			patch.object(ps_module, "_dispatch_subscription_event", side_effect=fail_after_partial_write),
			patch.object(frappe, "log_error") as core_log_error,
			patch("frappe.utils.sentry.capture_exception") as capture_exception,
		):
			self.assertEqual(
				self._deliver(payload),
				{"ok": False, "error": "subscription_event_unclaimed"},
			)
		core_log_error.assert_not_called()
		capture_exception.assert_not_called()

		event = frappe.get_doc(
			ps_module.SUBSCRIPTION_EVENT_DOCTYPE,
			{"provider_event_id": "txn-provider-failure"},
		)
		self.assertEqual(event.dispatch_status, "Unclaimed")
		self.assertFalse(frappe.db.exists("ToDo", {"description": partial_todo}))
		self.assertNotIn("must-not-be-persisted", event.redrive_payload)
		self.assertNotIn("also-private", event.redrive_payload)

		settings = frappe.get_doc("Payrexx Settings", self.settings_name)
		with (
			ps_module.as_automation_user(settings),
			patch.object(ps_module, "_dispatch_subscription_event", return_value=True) as dispatch,
		):
			result = ps_module._redrive_unclaimed_subscription_events(settings)

		self.assertEqual(result, {"retried": 1, "claimed": 1, "failed": 0})
		event.reload()
		self.assertEqual(event.dispatch_status, "Claimed")
		dispatch.assert_called_once()
