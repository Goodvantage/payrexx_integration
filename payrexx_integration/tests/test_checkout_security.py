import tomllib
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import CallbackManager
from requests import HTTPError, Response

from payrexx_integration import api, error_logging
from payrexx_integration.patches.v16_1 import backfill_automation_user
from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
	payrexx_settings as settings_module,
)
from payrexx_integration.payrexx_integration.payrexx.payrexx_client import (
	PayrexxAPIError,
	PayrexxClient,
	_normalize_api_base_domain,
)

CLIENT_MODULE = "payrexx_integration.payrexx_integration.payrexx.payrexx_client"
APP_ROOT = Path(__file__).resolve().parents[2]


class TestDependencyManifest(UnitTestCase):
	def test_python_manifest_declares_direct_http_runtime_import(self):
		project = tomllib.loads((APP_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
		self.assertIn("requests~=2.33.0", project["dependencies"])


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


class TestAutomationUserMigration(UnitTestCase):
	def test_valid_legacy_user_only_backfills_empty_rows_once(self):
		with (
			patch.object(backfill_automation_user.frappe.db, "exists", return_value=True),
			patch.object(
				backfill_automation_user.frappe,
				"get_meta",
				return_value=Mock(has_field=Mock(return_value=True)),
			),
			patch.object(
				backfill_automation_user.frappe.db,
				"get_single_value",
				return_value="automation@example.test",
			),
			patch.object(backfill_automation_user, "is_valid_automation_user", return_value=True),
			patch.object(
				backfill_automation_user.frappe,
				"get_all",
				side_effect=(["Live"], []),
			),
			patch.object(backfill_automation_user.frappe.db, "set_value") as set_value,
		):
			backfill_automation_user.execute()
			backfill_automation_user.execute()

		set_value.assert_called_once_with(
			"Payrexx Settings",
			"Live",
			"automation_user",
			"automation@example.test",
			update_modified=False,
		)

	def test_invalid_legacy_user_does_not_create_an_administrator_fallback(self):
		with (
			patch.object(backfill_automation_user.frappe.db, "exists", return_value=True),
			patch.object(
				backfill_automation_user.frappe,
				"get_meta",
				return_value=Mock(has_field=Mock(return_value=True)),
			),
			patch.object(backfill_automation_user.frappe.db, "get_single_value", return_value=None),
			patch.object(backfill_automation_user, "is_valid_automation_user", return_value=False),
			patch.object(backfill_automation_user.frappe.db, "set_value") as set_value,
		):
			backfill_automation_user.execute()

		set_value.assert_not_called()


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

	def test_gateway_retrieval_404_does_not_fall_back_to_default_domain(self):
		response = Response()
		response.status_code = 404
		with (
			patch(f"{CLIENT_MODULE}.frappe.conf", {"payrexx_allowed_api_hosts": ["api.pay.example"]}),
			patch(f"{CLIENT_MODULE}._execute_request", side_effect=HTTPError(response=response)) as request,
		):
			client = PayrexxClient(
				instance="demo",
				api_secret="secret",
				api_base_domain="pay.example",
			)
			with self.assertRaises(HTTPError):
				client.retrieve_gateway(123)

		request.assert_called_once()
		self.assertIn("api.pay.example", request.call_args.args[1])

	def test_partner_ping_rejects_gateway_zero_404_without_fallback(self):
		response = Response()
		response.status_code = 404
		response.url = "https://api.pay.goodvantage.ch/v1.16/Gateway/0/?instance=goodvantage"
		response.headers["Content-Type"] = "application/json"
		response._content = b'{"status":"error","message":"An error occurred: No Gateway found with id 0"}'

		with (
			patch(
				f"{CLIENT_MODULE}.frappe.conf",
				{"payrexx_allowed_api_hosts": ["api.pay.goodvantage.ch"]},
			),
			patch(f"{CLIENT_MODULE}._execute_request", side_effect=HTTPError(response=response)) as request,
		):
			with self.assertRaises(HTTPError):
				PayrexxClient(
					instance="goodvantage",
					api_secret="sk_test_dummy",
					api_base_domain="pay.goodvantage.ch",
				).ping_gateway()

		request.assert_called_once()
		self.assertEqual(request.call_args.args[1], response.url)

	def test_ping_rejects_every_http_200_near_match_envelope(self):
		client = PayrexxClient(instance="demo", api_secret="sk_test_dummy")
		near_matches = (
			{"status": "success", "data": []},
			{"status": "error", "message": "An error occurred: No Gateway found with id 0"},
			{"status": "error", "message": "No Gateway found with id 0", "data": []},
			{"status": "error", "message": "No Gateway found with id 00"},
		)
		for body in near_matches:
			with (
				self.subTest(body=body),
				patch(f"{CLIENT_MODULE}._execute_request", return_value=body),
				patch(f"{CLIENT_MODULE}.log_sanitized_error"),
				self.assertRaises(PayrexxAPIError),
			):
				client.ping_gateway()

	def test_gateway_retrieval_auth_rejection_still_falls_back(self):
		for status_code in (401, 403):
			with self.subTest(status_code=status_code):
				response = Response()
				response.status_code = status_code
				with (
					patch(
						f"{CLIENT_MODULE}.frappe.conf",
						{"payrexx_allowed_api_hosts": ["api.pay.example"]},
					),
					patch(
						f"{CLIENT_MODULE}._execute_request",
						side_effect=(
							HTTPError(response=response),
							{"status": "success", "data": [{"id": 123}]},
						),
					) as request,
				):
					client = PayrexxClient(
						instance="demo",
						api_secret="secret",
						api_base_domain="pay.example",
					)
					self.assertEqual(client.retrieve_gateway(123)["id"], 123)

				self.assertEqual(request.call_count, 2)
				self.assertIn("api.payrexx.com", request.call_args_list[1].args[1])

	def test_gateway_create_404_still_falls_back(self):
		response = Response()
		response.status_code = 404
		with (
			patch(f"{CLIENT_MODULE}.frappe.conf", {"payrexx_allowed_api_hosts": ["api.pay.example"]}),
			patch(
				f"{CLIENT_MODULE}._execute_request",
				side_effect=(
					HTTPError(response=response),
					{"status": "success", "data": [{"id": 123}]},
				),
			) as request,
		):
			client = PayrexxClient(
				instance="demo",
				api_secret="secret",
				api_base_domain="pay.example",
			)
			self.assertEqual(client.create_gateway({"amount": 100})["id"], 123)

		self.assertEqual(request.call_count, 2)
		self.assertIn("api.payrexx.com", request.call_args_list[1].args[1])

	def test_failed_custom_host_fallback_logs_only_the_final_failure(self):
		custom_response = Response()
		custom_response.status_code = 401
		custom_response.reason = "Unauthorized"
		custom_response.url = "https://api.pay.example/v1.16/Gateway/?instance=demo"
		canonical_response = Response()
		canonical_response.status_code = 503
		canonical_response.reason = "Unavailable"
		canonical_response.url = "https://api.payrexx.com/v1.16/Gateway/?instance=demo"
		with (
			patch(f"{CLIENT_MODULE}.frappe.conf", {"payrexx_allowed_api_hosts": ["api.pay.example"]}),
			patch(
				f"{CLIENT_MODULE}.get_request_session",
				side_effect=(
					_StubProviderSession(custom_response),
					_StubProviderSession(canonical_response),
				),
			),
			patch(f"{CLIENT_MODULE}.log_sanitized_error") as log_error,
			self.assertRaises(HTTPError),
		):
			PayrexxClient(
				instance="demo",
				api_secret="secret",
				api_base_domain="pay.example",
			).create_gateway({"amount": 100})

		log_error.assert_called_once()
		self.assertEqual(log_error.call_args.kwargs, {"http_status": 503})


class _StubProviderSession:
	"""Real request preparation, canned response.

	Keeps the client's auth callable on the actual wire path (so the header is
	really attached) while making the failure deterministic and offline.
	"""

	def __init__(self, response):
		self._session = requests.Session()
		self._response = response
		self.send_kwargs = None

	def prepare_request(self, request):
		prepared_request = self._session.prepare_request(request)
		self._response.request = prepared_request
		return prepared_request

	def merge_environment_settings(self, *args, **kwargs):
		return self._session.merge_environment_settings(*args, **kwargs)

	def send(self, prepared_request, **kwargs):
		self.send_kwargs = kwargs
		return self._response


class TestSafePayUrlBoundary(UnitTestCase):
	API_SECRET = "sk_live_pay_url_boundary_secret"
	PAYER_EMAIL = "payer-boundary@example.test"
	PROVIDER_URL = "https://merchant.payrexx.com/checkout?token=private-checkout-token"
	EXCEPTION_TEXT = f"provider rejected {PAYER_EMAIL} at {PROVIDER_URL} using credential {API_SECRET}"

	def _assert_sanitized_error_log(self, log_error):
		log_error.assert_called_once()
		self.assertEqual(log_error.call_args.args[0], "payrexx_pay_url")
		self.assertIsInstance(log_error.call_args.args[1], RuntimeError)
		self.assertEqual(log_error.call_args.kwargs, {})

	def test_failure_logs_only_the_bounded_sanitized_contract(self):
		with (
			patch.object(api, "payrexx_pay_url", side_effect=RuntimeError(self.EXCEPTION_TEXT)),
			patch.object(api, "log_sanitized_error") as log_error,
		):
			self.assertEqual(api.safe_pay_url("SINV-BOUNDARY-TEST"), "")

		self._assert_sanitized_error_log(log_error)

	def test_gateway_resolution_failure_uses_the_same_sanitized_contract(self):
		with (
			patch.object(api.frappe.db, "exists", return_value=True),
			patch.object(api.frappe, "get_doc", return_value=_sales_invoice()),
			patch.object(api, "resolve_payrexx_settings", side_effect=RuntimeError(self.EXCEPTION_TEXT)),
			patch.object(api, "log_sanitized_error") as log_error,
		):
			self.assertEqual(api.payrexx_pay_url("SINV-BOUNDARY-TEST"), "")

		self._assert_sanitized_error_log(log_error)

	def test_partially_paid_invoice_returns_no_link_without_gateway_resolution(self):
		for outstanding_amount in (0, 60):
			with (
				self.subTest(outstanding_amount=outstanding_amount),
				patch.object(api.frappe.db, "exists", return_value=True),
				patch.object(
					api.frappe,
					"get_doc",
					return_value=_sales_invoice(outstanding_amount=outstanding_amount),
				),
				patch.object(api, "resolve_payrexx_settings") as resolve_settings,
			):
				self.assertEqual(api.safe_pay_url("SINV-SECURITY-TEST"), "")

			resolve_settings.assert_not_called()

	def test_retryable_database_errors_propagate_without_logging(self):
		for error_type in (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
			error = error_type(f"retryable failure containing {self.PAYER_EMAIL}")
			with (
				self.subTest(error_type=error_type.__name__),
				patch.object(api.frappe.db, "exists", return_value=True),
				patch.object(api.frappe, "get_doc", return_value=_sales_invoice()),
				patch.object(api, "resolve_payrexx_settings", side_effect=error),
				patch.object(api, "log_sanitized_error") as log_error,
				self.assertRaises(error_type) as raised,
			):
				api.safe_pay_url("SINV-BOUNDARY-TEST")

			self.assertIs(raised.exception, error)
			log_error.assert_not_called()


class TestApiSecretNeverReachesLoggedTracebacks(UnitTestCase):
	"""Regression guard for audit finding V-H1 (2026-07-30).

	Provider failures must cross only the sanitized app-local logging seam. Core
	traceback logging and Sentry capture are forbidden on this path.
	"""

	API_SECRET = "sk_live_payrexx_secret_leak_regression"
	PAYER_EMAIL = "payer-leak-regression@example.test"

	def _sanitized_reports_by(self, provider_call):
		logged = []

		def capture_report(operation, exception, **kwargs):
			logged.append((operation, type(exception).__name__, kwargs))

		with (
			patch(f"{CLIENT_MODULE}.log_sanitized_error", side_effect=capture_report),
			patch.object(frappe, "log_error") as core_log_error,
			patch("frappe.utils.sentry.capture_exception") as capture_exception,
			self.assertRaises(Exception),
		):
			provider_call()

		self.assertEqual(len(logged), 1)
		core_log_error.assert_not_called()
		capture_exception.assert_not_called()
		return logged

	def test_client_does_not_retain_the_api_secret_as_an_attribute(self):
		client = PayrexxClient(instance="demo", api_secret=self.API_SECRET)

		# Traceback dumps expand plain objects, so an attribute leaks like a local.
		self.assertNotIn(self.API_SECRET, repr(vars(client)))

	def test_connection_failure_traceback_never_contains_the_api_secret(self):
		client = PayrexxClient(instance="demo", api_secret=self.API_SECRET)

		with patch.object(PayrexxClient, "_url", return_value="https://127.0.0.1:1/v1.14/Gateway/0/"):
			logged = self._sanitized_reports_by(client.ping_gateway)

		self.assertNotIn(self.API_SECRET, frappe.as_json(logged))

	def test_http_error_traceback_never_contains_the_api_secret_or_payer_data(self):
		response = Response()
		response.status_code = 401
		response.reason = "Unauthorized"
		response.url = "https://api.payrexx.com/v1.14/Gateway/?instance=demo"
		client = PayrexxClient(instance="demo", api_secret=self.API_SECRET)

		with patch(f"{CLIENT_MODULE}.get_request_session", return_value=_StubProviderSession(response)):
			logged = self._sanitized_reports_by(
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
		serialized = frappe.as_json(logged)
		self.assertNotIn(self.API_SECRET, serialized)
		self.assertNotIn(self.PAYER_EMAIL, serialized)

	def test_provider_failure_error_log_is_explicit_bounded_and_sanitized(self):
		provider_url = "https://api.payrexx.com/v1.16/Gateway/?instance=demo&token=private"
		exception_text = f"provider rejected {self.PAYER_EMAIL} using {self.API_SECRET}"
		response = Response()
		response.status_code = 503
		response.reason = exception_text
		response.url = provider_url
		client = PayrexxClient(instance="demo", api_secret=self.API_SECRET)
		with (
			patch(f"{CLIENT_MODULE}.get_request_session", return_value=_StubProviderSession(response)),
			patch(f"{CLIENT_MODULE}.log_sanitized_error") as log_error,
			patch.object(frappe, "log_error") as core_log_error,
			self.assertRaises(HTTPError),
		):
			client.create_gateway(
				{
					"amount": 10000,
					"referenceId": "IR-LOG-BOUNDARY",
					"fields[email][value]": self.PAYER_EMAIL,
				}
			)

		log_error.assert_called_once()
		self.assertEqual(log_error.call_args.args[0], "payrexx_request")
		self.assertIsInstance(log_error.call_args.args[1], HTTPError)
		self.assertEqual(log_error.call_args.kwargs, {"http_status": 503})
		core_log_error.assert_not_called()

	def test_provider_request_uses_bounded_connect_and_read_timeout(self):
		response = Response()
		response.status_code = 200
		response.headers["content-type"] = "application/json"
		response._content = b'{"status":"error","message":"No Gateway found with id 0"}'
		session = _StubProviderSession(response)
		client = PayrexxClient(instance="demo", api_secret=self.API_SECRET)

		with patch(f"{CLIENT_MODULE}.get_request_session", return_value=session):
			self.assertEqual(
				client.ping_gateway(),
				{"status": "error", "message": "No Gateway found with id 0"},
			)

		self.assertEqual(session.send_kwargs["timeout"], (5, 30))

	def test_timeout_makes_one_request_without_custom_host_fallback_and_logs_status_none(self):
		provider_url = "https://api.pay.example/v1.16/Gateway/?instance=demo"
		exception_text = (
			f"timeout contacting {provider_url} for {self.PAYER_EMAIL} using credential {self.API_SECRET}"
		)
		session = _StubProviderSession(Response())
		session.send = Mock(side_effect=requests.Timeout(exception_text))
		with (
			patch(f"{CLIENT_MODULE}.frappe.conf", {"payrexx_allowed_api_hosts": ["api.pay.example"]}),
			patch(f"{CLIENT_MODULE}.get_request_session", return_value=session),
			patch(f"{CLIENT_MODULE}.log_sanitized_error") as log_error,
			patch.object(frappe, "log_error") as core_log_error,
			self.assertRaises(requests.Timeout),
		):
			PayrexxClient(
				instance="demo",
				api_secret=self.API_SECRET,
				api_base_domain="pay.example",
			).create_gateway(
				{
					"amount": 10000,
					"referenceId": "IR-TIMEOUT-BOUNDARY",
					"fields[email][value]": self.PAYER_EMAIL,
				}
			)

		session.send.assert_called_once()
		self.assertEqual(session.send.call_args.args[0].url, provider_url)
		log_error.assert_called_once()
		self.assertEqual(log_error.call_args.args[0], "payrexx_request")
		self.assertIsInstance(log_error.call_args.args[1], requests.Timeout)
		self.assertEqual(log_error.call_args.kwargs, {"http_status": None})
		core_log_error.assert_not_called()


class TestCheckoutProviderFailureLogging(IntegrationTestCase):
	API_SECRET = "sk_live_outer_checkout_secret"
	PAYER_EMAIL = "outer-checkout-payer@example.test"
	PROVIDER_URL = "https://api.payrexx.com/v1.16/Gateway/?instance=demo&token=provider-private"
	PROVIDER_RESPONSE = f"provider response for {PAYER_EMAIL} with credential {API_SECRET}"

	def test_outer_checkout_keeps_one_context_free_error_log(self):
		response = Response()
		response.status_code = 503
		response.reason = self.PROVIDER_RESPONSE
		response.url = self.PROVIDER_URL
		client = PayrexxClient(instance="demo", api_secret=self.API_SECRET)
		settings = Mock()
		settings.name = "Live"
		settings._validate_payment_request_source = Mock()
		settings._client.return_value = client
		settings._build_create_gateway_payload.return_value = {
			"amount": 10000,
			"currency": "CHF",
			"referenceId": "IR-OUTER-LOG-TEST",
			"fields[email][value]": self.PAYER_EMAIL,
		}
		integration_request = Mock()
		integration_request.name = "IR-OUTER-LOG-TEST"
		integration_request.data = frappe.as_json({"reference_docname": "PR-OUTER-LOG-TEST"})
		fingerprint = error_logging.error_fingerprint("payrexx_request", "HTTPError")
		before = set(frappe.get_all("Error Log", filters={"fingerprint": fingerprint}, pluck="name"))

		with (
			patch.object(settings_module, "as_automation_user", return_value=nullcontext()),
			patch.object(settings_module, "_create_integration_request", return_value=integration_request),
			patch.object(settings_module, "_log_unknown_gateway_outcome"),
			patch(f"{CLIENT_MODULE}.get_request_session", return_value=_StubProviderSession(response)),
			patch.object(error_logging, "_must_defer_database_log", return_value=False),
			patch.object(error_logging, "_log_to_file"),
			patch.object(frappe, "log_error") as core_log_error,
			patch.object(frappe, "get_traceback") as get_traceback,
			patch("frappe.utils.sentry.capture_exception") as capture_exception,
			self.assertRaises(frappe.ValidationError),
		):
			settings_module.PayrexxSettings.get_payment_url(
				settings,
				reference_doctype="Payment Request",
				reference_docname="PR-OUTER-LOG-TEST",
				payer_email=self.PAYER_EMAIL,
				amount=100,
				currency="CHF",
			)

		core_log_error.assert_not_called()
		get_traceback.assert_not_called()
		capture_exception.assert_not_called()
		self.assertEqual(response.request.headers["x-api-key"], self.API_SECRET)
		after = set(frappe.get_all("Error Log", filters={"fingerprint": fingerprint}, pluck="name"))
		created = after - before
		for created_name in created:
			self.addCleanup(frappe.db.delete, "Error Log", {"name": created_name})
		self.assertEqual(len(created), 1)
		name = created.pop()
		row = frappe.db.get_value(
			"Error Log", name, ["method", "error", "metadata", "fingerprint"], as_dict=True
		)
		self.assertEqual(row.method, "Payrexx request failed")
		self.assertEqual(row.metadata, "{}")
		self.assertEqual(row.fingerprint, fingerprint)
		self.assertLessEqual(len(row.method), 140)
		self.assertLessEqual(len(row.error), 300)
		persisted = frappe.as_json(row)
		for sensitive_value in (
			self.PROVIDER_RESPONSE,
			self.PROVIDER_URL,
			self.PAYER_EMAIL,
			self.API_SECRET,
			"provider-private",
		):
			self.assertNotIn(sensitive_value, persisted)

	def test_outer_checkout_propagates_retryable_database_errors_without_logging(self):
		settings = Mock()
		settings.name = "Live"
		settings._validate_payment_request_source = Mock()
		settings._client.side_effect = frappe.QueryTimeoutError("retry complete transaction")
		with (
			patch.object(settings_module, "as_automation_user", return_value=nullcontext()),
			patch.object(error_logging, "log_sanitized_error") as log_error,
			patch.object(frappe, "log_error") as core_log_error,
			self.assertRaises(frappe.QueryTimeoutError),
		):
			settings_module.PayrexxSettings.get_payment_url(settings)

		log_error.assert_not_called()
		core_log_error.assert_not_called()


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
				},
				"Live",
			)

		self.assertIs(created, document)
		document.insert.assert_called_once_with(ignore_permissions=True)
		commit.assert_not_called()
		self.assertEqual(get_doc.call_args.args[0]["status"], "Queued")
		request_data = frappe.parse_json(get_doc.call_args.args[0]["data"])
		self.assertEqual(request_data["payrexx_settings"], "Live")
		self.assertEqual(
			request_data[settings_module.PAYREXX_SUCCESS_TOKEN_VERSION_KEY],
			settings_module.PAYREXX_SUCCESS_TOKEN_VERSION,
		)

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
			patch.object(settings_module, "as_automation_user", return_value=nullcontext()),
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
			patch.object(settings_module, "as_automation_user", return_value=nullcontext()),
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
	def setUp(self):
		super().setUp()
		self.enterContext(patch.dict(api.frappe.local.conf, {"encryption_key": "payrexx-test-signing-key"}))

	def test_tampered_success_token_is_rejected_before_request_lookup(self):
		with (
			patch.object(api.frappe, "get_doc") as get_doc,
			self.assertRaises(frappe.PermissionError) as exc,
		):
			api.payment_success(
				ir="IR-MARKED",
				gateway_name="Live",
				token="tampered",
			)

		self.assertEqual(str(exc.exception), "Invalid payment return")
		get_doc.assert_not_called()

	def test_success_return_does_not_expose_unknown_or_marked_references(self):
		valid_token = api.sign_payment_success_reference("IR-UNKNOWN", "Live")
		with (
			patch.object(api.frappe, "get_doc", side_effect=frappe.DoesNotExistError),
			self.assertRaises(frappe.PermissionError) as unknown,
		):
			api.payment_success(ir="IR-UNKNOWN", gateway_name="Live", token=valid_token)

		marked_request = frappe._dict(
			integration_request_service="Payrexx",
			data=frappe.as_json(
				{
					settings_module.PAYREXX_SUCCESS_TOKEN_VERSION_KEY: 1,
					"payrexx_settings": "Live",
				}
			),
		)
		with (
			patch.object(api.frappe, "get_doc", return_value=marked_request),
			self.assertRaises(frappe.PermissionError) as unsigned,
		):
			api.payment_success(ir="IR-MARKED", gateway_name="Live")

		self.assertEqual(str(unknown.exception), str(unsigned.exception))

	def test_success_token_cannot_select_another_gateway(self):
		marked_request = frappe._dict(
			integration_request_service="Payrexx",
			data=frappe.as_json(
				{
					settings_module.PAYREXX_SUCCESS_TOKEN_VERSION_KEY: 1,
					"payrexx_settings": "Live",
				}
			),
		)
		with (
			patch.object(api.frappe, "get_doc", return_value=marked_request),
			patch.object(settings_module, "reconcile_integration_request") as reconcile,
			self.assertRaises(frappe.PermissionError),
		):
			api.payment_success(
				ir="IR-MARKED",
				gateway_name="Sandbox",
				token=api.sign_payment_success_reference("IR-MARKED", "Sandbox"),
			)

		reconcile.assert_not_called()

	def test_present_unsupported_success_marker_versions_fail_closed(self):
		valid_token = api.sign_payment_success_reference("IR-MARKED", "Live")
		for marker_version in (None, 0, "", 2, "1", True):
			marked_request = frappe._dict(
				integration_request_service="Payrexx",
				data=frappe.as_json(
					{
						settings_module.PAYREXX_SUCCESS_TOKEN_VERSION_KEY: marker_version,
						"payrexx_settings": "Live",
					}
				),
			)
			with (
				self.subTest(marker_version=marker_version),
				patch.object(api.frappe, "get_doc", return_value=marked_request),
				patch.object(settings_module, "reconcile_integration_request") as reconcile,
				self.assertRaises(frappe.PermissionError),
			):
				api.payment_success(
					ir="IR-MARKED",
					gateway_name="Live",
					token=valid_token,
				)
			reconcile.assert_not_called()

	def test_valid_marked_success_return_reconciles(self):
		marked_request = Mock(
			integration_request_service="Payrexx",
			data=frappe.as_json(
				{
					settings_module.PAYREXX_SUCCESS_TOKEN_VERSION_KEY: 1,
					"payrexx_settings": "Live",
				}
			),
			status="Queued",
		)
		original_response = getattr(api.frappe.local, "response", None)
		try:
			api.frappe.local.response = {}
			with (
				patch.object(api.frappe, "get_doc", return_value=marked_request),
				patch.object(
					settings_module, "reconcile_integration_request", return_value=False
				) as reconcile,
				patch.object(api, "_payment_failed_redirect_url", return_value="/payment-failed"),
			):
				api.payment_success(
					ir="IR-MARKED",
					gateway_name="Live",
					token=api.sign_payment_success_reference("IR-MARKED", "Live"),
				)
		finally:
			api.frappe.local.response = original_response or {}

		reconcile.assert_called_once_with("IR-MARKED", gateway_name="Live")

	def test_unsigned_unmarked_legacy_success_return_remains_compatible(self):
		legacy_request = Mock(
			integration_request_service="Payrexx",
			data="{}",
			status="Queued",
		)
		original_response = getattr(api.frappe.local, "response", None)
		try:
			api.frappe.local.response = {}
			with (
				patch.object(api.frappe, "get_doc", return_value=legacy_request),
				patch.object(
					settings_module, "reconcile_integration_request", return_value=False
				) as reconcile,
				patch.object(api, "_payment_failed_redirect_url", return_value="/payment-failed"),
			):
				api.payment_success(ir="IR-LEGACY", gateway_name="Live")
		finally:
			api.frappe.local.response = original_response or {}

		reconcile.assert_called_once_with("IR-LEGACY", gateway_name="Live")
		legacy_request.reload.assert_called_once_with()

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


class TestPayLinkKeyCompatibility(UnitTestCase):
	def test_legacy_raw_key_token_still_verifies(self) -> None:
		# Links issued before the purpose-scoped signing key (D53) were
		# signed with the raw site key; the documented contract says they
		# keep verifying.
		import hashlib
		import hmac as hmac_module

		from payrexx_integration.api import _raw_site_key, sign_reference, verify_reference

		payload = "ACC-SINV-2026-00001|gateway-1"
		legacy = hmac_module.new(_raw_site_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
		self.assertTrue(verify_reference(payload, legacy))
		self.assertTrue(verify_reference(payload, sign_reference(payload)))
		self.assertNotEqual(legacy, sign_reference(payload))
		self.assertFalse(verify_reference(payload, legacy[:-1] + ("0" if legacy[-1] != "0" else "1")))
