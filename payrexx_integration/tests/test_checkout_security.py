from unittest.mock import Mock, patch

import frappe
import requests
from frappe.tests import UnitTestCase
from frappe.utils import CallbackManager
from requests import Response

from payrexx_integration import api
from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
	payrexx_settings as settings_module,
)
from payrexx_integration.payrexx_integration.payrexx.payrexx_client import (
	PayrexxClient,
	_normalize_api_base_domain,
)

CLIENT_MODULE = "payrexx_integration.payrexx_integration.payrexx.payrexx_client"


def _sales_invoice(*, outstanding_amount=100, currency="CHF"):
	return frappe._dict(
		doctype="Sales Invoice",
		name="SINV-SECURITY-TEST",
		docstatus=1,
		is_return=0,
		company="Test Company",
		currency=currency,
		grand_total=100,
		rounded_total=0,
		outstanding_amount=outstanding_amount,
	)


def _payment_request(*, status="Requested", outstanding_amount=100, currency="CHF"):
	return frappe._dict(
		doctype="Payment Request",
		name="PR-SECURITY-TEST",
		docstatus=1,
		status=status,
		payment_request_type="Inward",
		payment_gateway="Payrexx-Live",
		company="Test Company",
		currency=currency,
		grand_total=100,
		outstanding_amount=outstanding_amount,
		reference_doctype="Sales Invoice",
		reference_name="SINV-SECURITY-TEST",
		payment_url="https://merchant.payrexx.com/checkout",
	)


def _integration_request_data(**overrides):
	data = {
		"amount": 100,
		"currency": "CHF",
		"payment_gateway": "Payrexx-Live",
		"reference_doctype": "Payment Request",
		"reference_docname": "PR-SECURITY-TEST",
		"payrexx_settings": "Live",
		"payrexx_gateway_id": 123,
		"payrexx_gateway_hash": "gateway-hash",
		"payrexx_checkout_url": "https://merchant.payrexx.com/checkout",
		"payrexx_gateway_amount": 10000,
		"payrexx_gateway_currency": "CHF",
	}
	data.update(overrides)
	return data


class TestPayrexxApiHostTrust(UnitTestCase):
	def test_canonical_production_sandbox_and_explicit_https_port_are_allowed(self):
		self.assertEqual(_normalize_api_base_domain("payrexx.com"), "payrexx.com")
		self.assertEqual(_normalize_api_base_domain("api.payrexx.com"), "payrexx.com")
		self.assertEqual(_normalize_api_base_domain("sandbox.payrexx.com"), "sandbox.payrexx.com")
		self.assertEqual(_normalize_api_base_domain("payrexx.com:443"), "payrexx.com")

	def test_malicious_or_malformed_api_hosts_are_rejected(self):
		values = (
			"https://api.payrexx.com",
			"user@payrexx.com",
			"payrexx.com/path",
			"payrexx.com?next=evil.example",
			"payrexx.com#evil.example",
			"127.0.0.1",
			"[::1]",
			"payrexx.com\n.evil.example",
			"-payrexx.com",
			"payrexx..com",
			"payrexx.com:",
			"payrexx.com:8443",
		)
		for value in values:
			with self.subTest(value=value), self.assertRaises(ValueError):
				_normalize_api_base_domain(value)

	def test_custom_platform_host_requires_exact_site_config_allowlist(self):
		with patch(f"{CLIENT_MODULE}.frappe.conf", {}), self.assertRaises(ValueError):
			_normalize_api_base_domain("pay.goodvantage.ch")

		with patch(
			f"{CLIENT_MODULE}.frappe.conf",
			{"payrexx_allowed_api_hosts": ["api.pay.goodvantage.ch"]},
		):
			self.assertEqual(_normalize_api_base_domain("pay.goodvantage.ch"), "pay.goodvantage.ch")

	def test_allowlist_rejects_wildcards_urls_and_non_list_configuration(self):
		for configured in (
			"api.pay.goodvantage.ch",
			["*.goodvantage.ch"],
			["https://api.pay.goodvantage.ch"],
		):
			with (
				self.subTest(configured=configured),
				patch(f"{CLIENT_MODULE}.frappe.conf", {"payrexx_allowed_api_hosts": configured}),
				self.assertRaises(ValueError),
			):
				_normalize_api_base_domain("pay.goodvantage.ch")

	def test_settings_does_not_read_api_secret_for_untrusted_host(self):
		settings = Mock()
		settings.get.return_value = "attacker.example"
		settings.get_password = Mock(return_value="must-not-be-read")

		with patch(f"{CLIENT_MODULE}.frappe.conf", {}), self.assertRaises(frappe.ValidationError):
			settings_module.PayrexxSettings._client(settings)

		settings.get_password.assert_not_called()

	def test_untrusted_client_never_contacts_provider(self):
		with (
			patch(f"{CLIENT_MODULE}.frappe.conf", {}),
			patch(f"{CLIENT_MODULE}._execute_request") as request,
			self.assertRaises(ValueError),
		):
			PayrexxClient(instance="demo", api_secret="secret", api_base_domain="attacker.example")

		request.assert_not_called()


class _StubProviderSession:
	"""Real request preparation, canned response.

	Keeps the client's auth callable on the actual wire path (so the header is
	really attached) while making the failure deterministic and offline.
	"""

	def __init__(self, response):
		self._session = requests.Session()
		self._response = response

	def prepare_request(self, request):
		prepared_request = self._session.prepare_request(request)
		self._response.request = prepared_request
		return prepared_request

	def merge_environment_settings(self, *args, **kwargs):
		return self._session.merge_environment_settings(*args, **kwargs)

	def send(self, prepared_request, **kwargs):
		return self._response


class TestApiSecretNeverReachesLoggedTracebacks(UnitTestCase):
	"""Regression guard for audit finding V-H1 (2026-07-30).

	Every failed provider request is written to Error Log — and to Sentry when
	telemetry is on — with the frame variables of the failing frames. Frappe's
	sanitizer redacts only the exact dict keys password/passwd/secret/token/key/
	pwd, so an `{"x-api-key": <secret>}` header dict (or a client attribute
	holding the secret) is persisted verbatim. The client must therefore keep the
	secret and the payer payload out of every frame variable of the request path.
	"""

	API_SECRET = "sk_live_payrexx_secret_leak_regression"
	PAYER_EMAIL = "payer-leak-regression@example.test"

	def _tracebacks_logged_by(self, provider_call):
		"""Return exactly what frappe would persist for a failing provider call."""
		logged = []

		def capture_traceback(*args, **kwargs):
			logged.append(frappe.get_traceback(with_context=True))

		original_request_doc = frappe.flags.integration_request_doc
		frappe.flags.integration_request_doc = None
		try:
			with (
				patch.object(frappe, "log_error", side_effect=capture_traceback),
				self.assertRaises(Exception),
			):
				provider_call()
		finally:
			frappe.flags.integration_request_doc = original_request_doc

		self.assertTrue(logged, "the failing provider request was not reported at all")
		return logged

	def test_client_does_not_retain_the_api_secret_as_an_attribute(self):
		client = PayrexxClient(instance="demo", api_secret=self.API_SECRET)

		# Traceback dumps expand plain objects, so an attribute leaks like a local.
		self.assertNotIn(self.API_SECRET, repr(vars(client)))

	def test_connection_failure_traceback_never_contains_the_api_secret(self):
		client = PayrexxClient(instance="demo", api_secret=self.API_SECRET)

		with patch.object(PayrexxClient, "_url", return_value="https://127.0.0.1:1/v1.14/Gateway/0/"):
			logged = self._tracebacks_logged_by(client.ping_gateway)

		for traceback_text in logged:
			self.assertNotIn(self.API_SECRET, traceback_text)

	def test_http_error_traceback_never_contains_the_api_secret_or_payer_data(self):
		response = Response()
		response.status_code = 401
		response.reason = "Unauthorized"
		response.url = "https://api.payrexx.com/v1.14/Gateway/?instance=demo"
		client = PayrexxClient(instance="demo", api_secret=self.API_SECRET)

		with patch(f"{CLIENT_MODULE}.get_request_session", return_value=_StubProviderSession(response)):
			logged = self._tracebacks_logged_by(
				lambda: client.create_gateway(
					{
						"amount": 10000,
						"currency": "CHF",
						"referenceId": "IR-LEAK-REGRESSION",
						"fields[email][value]": self.PAYER_EMAIL,
					}
				)
			)

		# The header still goes out on the wire; it just never becomes a variable.
		self.assertEqual(response.request.headers["x-api-key"], self.API_SECRET)
		for traceback_text in logged:
			self.assertNotIn(self.API_SECRET, traceback_text)
			self.assertNotIn(self.PAYER_EMAIL, traceback_text)


class TestCheckoutCurrentState(UnitTestCase):
	def test_partially_paid_invoice_fails_before_payment_request_or_provider_creation(self):
		invoice = _sales_invoice(outstanding_amount=60)
		with (
			patch.object(settings_module, "_validate_gateway_currency"),
			patch.object(api.frappe, "get_doc", return_value=invoice),
			patch.object(api.frappe.db, "get_values") as get_payment_requests,
			self.assertRaises(frappe.ValidationError),
		):
			api._get_or_create_payment_request(invoice, "Live")

		get_payment_requests.assert_not_called()

	def test_partially_paid_payment_request_is_never_reused(self):
		invoice = _sales_invoice()
		payment_request = _payment_request(status="Partially Paid", outstanding_amount=40)
		payment_request.get_payment_url = Mock()
		active_request = frappe._dict(name="IR-SECURITY-TEST")
		with (
			patch.object(settings_module, "_validate_gateway_currency"),
			patch.object(api, "_get_active_checkout_requests", return_value=[active_request]),
			patch.object(
				api,
				"_get_active_payrexx_payment_requests",
				return_value=[frappe._dict(name=payment_request.name)],
			),
			patch.object(api.frappe, "get_doc", side_effect=(payment_request, invoice)),
			self.assertRaises(frappe.ValidationError),
		):
			api._get_payment_request_checkout_url(payment_request, invoice, "Live")

		payment_request.get_payment_url.assert_not_called()

	def test_existing_checkout_uses_settlement_lock_order(self):
		invoice = _sales_invoice()
		payment_request = _payment_request()
		active_request = frappe._dict(name="IR-SECURITY-TEST")
		lock_order = []

		def lock_checkout(_payment_request_name):
			lock_order.append("Integration Request")
			return [active_request]

		def lock_payment_requests(_sales_invoice_name, *, for_update):
			self.assertTrue(for_update)
			lock_order.append("Payment Request scan")
			return [frappe._dict(name=payment_request.name)]

		def lock_document(doctype, _name, *, for_update):
			self.assertTrue(for_update)
			lock_order.append(doctype)
			return payment_request if doctype == "Payment Request" else invoice

		with (
			patch.object(api, "_get_active_checkout_requests", side_effect=lock_checkout),
			patch.object(
				api,
				"_get_active_payrexx_payment_requests",
				side_effect=lock_payment_requests,
			),
			patch.object(api.frappe, "get_doc", side_effect=lock_document),
			patch.object(
				api,
				"_validate_payment_request_checkout_state",
				return_value=(10000, "CHF"),
			),
			patch.object(api, "_validated_checkout_url", return_value=payment_request.payment_url),
		):
			self.assertEqual(
				api._get_payment_request_checkout_url(payment_request, invoice, "Live"),
				payment_request.payment_url,
			)

		self.assertEqual(
			lock_order,
			["Integration Request", "Payment Request scan", "Payment Request", "Sales Invoice"],
		)

	def test_changed_amount_currency_or_source_is_rejected(self):
		invoice = _sales_invoice()
		changes = (
			{"grand_total": 90},
			{"currency": "EUR"},
			{"reference_name": "SINV-OTHER"},
		)
		for change in changes:
			payment_request = _payment_request()
			payment_request.update(change)
			with (
				self.subTest(change=change),
				patch.object(settings_module, "_validate_gateway_currency"),
				self.assertRaises(frappe.ValidationError),
			):
				settings_module._validate_payment_request_checkout_state(
					payment_request,
					invoice,
					expected_gateway="Payrexx-Live",
					require_submitted=True,
				)

	def test_checkout_reuse_requires_exact_persisted_amount_currency_and_source(self):
		payment_request = _payment_request()
		integration_request = frappe._dict(
			name="IR-SECURITY-TEST",
			status="Queued",
			reference_doctype="Payment Request",
			reference_docname=payment_request.name,
			data=frappe.as_json(_integration_request_data()),
		)
		with patch.object(settings_module, "_validate_gateway_currency"):
			self.assertEqual(
				api._validated_checkout_url(
					integration_request,
					payment_request,
					settings_name="Live",
					expected_amount=10000,
					expected_currency="CHF",
				),
				"https://merchant.payrexx.com/checkout",
			)

		for changed_data in (
			{"payrexx_gateway_amount": 9000},
			{"payrexx_gateway_currency": "EUR"},
			{"reference_docname": "PR-OTHER"},
		):
			integration_request.data = frappe.as_json(_integration_request_data(**changed_data))
			with (
				self.subTest(changed_data=changed_data),
				patch.object(settings_module, "_validate_gateway_currency"),
				self.assertRaises(frappe.ValidationError),
			):
				api._validated_checkout_url(
					integration_request,
					payment_request,
					settings_name="Live",
					expected_amount=10000,
					expected_currency="CHF",
				)


class TestAtomicCheckoutCreation(UnitTestCase):
	def test_app_owned_integration_request_insert_never_commits(self):
		document = Mock()
		with (
			patch.object(settings_module.frappe, "get_doc", return_value=document) as get_doc,
			patch.object(settings_module.frappe.db, "commit") as commit,
		):
			created = settings_module._create_integration_request(
				{
					"reference_doctype": "Payment Request",
					"reference_docname": "PR-SECURITY-TEST",
					"amount": 100,
				}
			)

		self.assertIs(created, document)
		document.insert.assert_called_once_with(ignore_permissions=True)
		commit.assert_not_called()
		self.assertEqual(get_doc.call_args.args[0]["status"], "Queued")

	def test_provider_failure_rolls_back_cleanly_and_retry_persists_complete_metadata(self):
		settings = Mock()
		settings.name = "Live"
		settings._validate_payment_request_source = Mock()
		settings._build_create_gateway_payload.return_value = {"amount": 10000, "currency": "CHF"}
		client = Mock()
		client.create_gateway.side_effect = (
			RuntimeError("provider unavailable"),
			{
				"id": 123,
				"hash": "gateway-hash",
				"link": "https://merchant.payrexx.com/checkout",
			},
		)
		settings._client.return_value = client
		request_data = {
			"reference_doctype": "Payment Request",
			"reference_docname": "PR-SECURITY-TEST",
			"amount": 100,
			"currency": "CHF",
		}
		first_request = Mock()
		first_request.name = "IR-FIRST"
		first_request.data = frappe.as_json(request_data)
		first_request.reference_docname = "PR-SECURITY-TEST"
		second_request = Mock()
		second_request.name = "IR-RETRY"
		second_request.data = frappe.as_json(request_data)
		second_request.reference_docname = "PR-SECURITY-TEST"

		with (
			patch.object(
				settings_module,
				"_create_integration_request",
				side_effect=(first_request, second_request),
			) as create_request,
			patch.object(settings_module, "_register_gateway_orphan_recovery") as register_recovery,
			patch.object(settings_module, "_log_unknown_gateway_outcome") as log_unknown,
			patch.object(settings_module.frappe, "log_error"),
			patch.object(settings_module.frappe.db, "commit") as commit,
			self.assertRaises(frappe.ValidationError),
		):
			settings_module.PayrexxSettings.get_payment_url(settings, **request_data)

		first_request.save.assert_not_called()
		log_unknown.assert_called_once_with("IR-FIRST", "Live")
		commit.assert_not_called()

		with (
			patch.object(
				settings_module,
				"_create_integration_request",
				return_value=second_request,
			) as retry_create_request,
			patch.object(settings_module, "_register_gateway_orphan_recovery") as retry_recovery,
			patch.object(settings_module.frappe, "log_error"),
			patch.object(settings_module.frappe.db, "commit") as retry_commit,
		):
			checkout_url = settings_module.PayrexxSettings.get_payment_url(settings, **request_data)

		self.assertEqual(checkout_url, "https://merchant.payrexx.com/checkout")
		self.assertEqual(create_request.call_count, 1)
		self.assertEqual(retry_create_request.call_count, 1)
		second_request.save.assert_called_once_with(ignore_permissions=True)
		persisted = frappe.parse_json(second_request.data)
		self.assertEqual(persisted["payrexx_gateway_id"], 123)
		self.assertEqual(persisted["payrexx_gateway_amount"], 10000)
		self.assertEqual(persisted["payrexx_gateway_currency"], "CHF")
		retry_recovery.assert_called_once()
		retry_commit.assert_not_called()
		register_recovery.assert_not_called()

	def test_rollback_recovery_log_uses_reference_and_id_without_checkout_secret(self):
		integration_request = frappe._dict(
			name="IR-ROLLBACK",
			reference_doctype="Payment Request",
			reference_docname="PR-SECURITY-TEST",
		)
		commit_callbacks = CallbackManager()
		rollback_callbacks = CallbackManager()
		logger = Mock()
		with (
			patch.object(settings_module.frappe.local.db, "after_commit", commit_callbacks),
			patch.object(settings_module.frappe.local.db, "after_rollback", rollback_callbacks),
			patch.object(settings_module.frappe, "logger", return_value=logger),
		):
			settings_module._register_gateway_orphan_recovery(
				integration_request,
				{
					"id": 123,
					"hash": "must-not-be-logged",
					"link": "https://merchant.payrexx.com/?secret=must-not-be-logged",
				},
				settings_name="Live",
			)
			rollback_callbacks.run()

		message = logger.critical.call_args.args[0]
		self.assertIn("IR-ROLLBACK", message)
		self.assertIn('"gateway_id": 123', message)
		self.assertNotIn("must-not-be-logged", message)
		pending_message = logger.warning.call_args.args[0]
		self.assertIn("state=local_commit_pending", pending_message)
		self.assertNotIn("must-not-be-logged", pending_message)
		logger.info.assert_not_called()

	def test_commit_failure_gap_keeps_unpaired_pending_recovery_evidence(self):
		integration_request = frappe._dict(
			name="IR-COMMIT-UNKNOWN",
			reference_doctype="Payment Request",
			reference_docname="PR-SECURITY-TEST",
		)
		commit_callbacks = CallbackManager()
		rollback_callbacks = CallbackManager()
		logger = Mock()
		with (
			patch.object(settings_module.frappe.local.db, "after_commit", commit_callbacks),
			patch.object(settings_module.frappe.local.db, "after_rollback", rollback_callbacks),
			patch.object(settings_module.frappe, "logger", return_value=logger),
		):
			settings_module._register_gateway_orphan_recovery(
				integration_request,
				{"id": 456, "hash": "not-logged", "link": "https://pay.example/secret"},
				settings_name="Live",
			)
			# Frappe clears rollback callbacks before issuing SQL COMMIT. If that
			# SQL call fails, neither outcome callback runs; the immediate journal
			# entry is the only conservative durable recovery evidence.
			rollback_callbacks.reset()
			commit_callbacks.reset()
			rollback_callbacks.run()

		pending_message = logger.warning.call_args.args[0]
		self.assertIn("state=local_commit_pending", pending_message)
		self.assertIn("IR-COMMIT-UNKNOWN", pending_message)
		self.assertNotIn("not-logged", pending_message)
		self.assertNotIn("secret", pending_message)
		logger.info.assert_not_called()
		logger.critical.assert_not_called()

	def test_successful_commit_pairs_pending_recovery_evidence(self):
		integration_request = frappe._dict(
			name="IR-COMMIT-CONFIRMED",
			reference_doctype="Payment Request",
			reference_docname="PR-SECURITY-TEST",
		)
		commit_callbacks = CallbackManager()
		rollback_callbacks = CallbackManager()
		logger = Mock()
		with (
			patch.object(settings_module.frappe.local.db, "after_commit", commit_callbacks),
			patch.object(settings_module.frappe.local.db, "after_rollback", rollback_callbacks),
			patch.object(settings_module.frappe, "logger", return_value=logger),
		):
			settings_module._register_gateway_orphan_recovery(
				integration_request,
				{"id": 789, "hash": "not-logged", "link": "https://pay.example/secret"},
				settings_name="Live",
			)
			commit_callbacks.run()

		self.assertIn("state=local_commit_pending", logger.warning.call_args.args[0])
		self.assertIn("state=local_commit_confirmed", logger.info.call_args.args[0])
		self.assertNotIn("not-logged", logger.info.call_args.args[0])
		self.assertNotIn("secret", logger.info.call_args.args[0])
		logger.critical.assert_not_called()


class TestCheckoutRetryBoundary(UnitTestCase):
	def test_deadlock_retries_complete_boundary_before_provider_contact(self):
		operation = Mock(
			side_effect=(
				frappe.QueryDeadlockError((1213, "Deadlock found when trying to get lock")),
				"https://pay.example/checkout",
			)
		)
		with (
			patch.object(api.frappe.db, "rollback") as rollback,
			patch.object(api.time, "sleep") as sleep,
		):
			self.assertEqual(
				api._run_checkout_with_deadlock_retry(operation),
				"https://pay.example/checkout",
			)

		self.assertEqual(operation.call_count, 2)
		rollback.assert_called_once_with()
		sleep.assert_called_once_with(0.25)

	def test_deadlock_after_provider_contact_is_rolled_back_but_never_replayed(self):
		def operation():
			api.frappe.flags[settings_module.CHECKOUT_PROVIDER_CONTACT_FLAG] = True
			raise frappe.QueryDeadlockError((1213, "Deadlock after provider response"))

		with (
			patch.object(api.frappe.db, "rollback") as rollback,
			patch.object(api.time, "sleep") as sleep,
			self.assertRaises(frappe.QueryDeadlockError),
		):
			api._run_checkout_with_deadlock_retry(operation)

		rollback.assert_called_once_with()
		sleep.assert_not_called()


class TestBrowserReturnTransactionBinding(UnitTestCase):
	def test_confirmed_transaction_must_reference_expected_integration_request(self):
		gateway = {
			"referenceId": "IR-OTHER",
			"invoices": [
				{
					"referenceId": "IR-OTHER",
					"currency": "CHF",
					"transactions": [{"id": 1, "status": "confirmed", "amount": 10000}],
				}
			],
		}
		self.assertEqual(
			settings_module._confirmed_transaction_from_gateway(gateway, "IR-EXPECTED"),
			{},
		)

	def test_matching_transaction_is_selected_even_after_mismatched_confirmation(self):
		gateway = {
			"invoices": [
				{
					"referenceId": "IR-OTHER",
					"transactions": [{"id": 1, "status": "confirmed", "amount": 10000}],
				},
				{
					"referenceId": "IR-EXPECTED",
					"currency": "CHF",
					"transactions": [{"id": 2, "status": "confirmed", "amount": 10000}],
				},
			],
		}

		transaction = settings_module._confirmed_transaction_from_gateway(gateway, "IR-EXPECTED")

		self.assertEqual(transaction["id"], 2)
		self.assertEqual(transaction["referenceId"], "IR-EXPECTED")
		self.assertEqual(transaction["currency"], "CHF")
