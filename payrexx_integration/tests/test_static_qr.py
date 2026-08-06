# Copyright (c) 2026, Goodvantage GmbH and contributors
# See license.txt

from contextlib import nullcontext
from unittest.mock import Mock, patch

import frappe
import requests
from frappe.tests import IntegrationTestCase
from requests import HTTPError, Response

from payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings import (
	QR_DELETE_TOLERATED_STATUSES,
	_sanitize_qr_session_value,
	_validate_qr_code_uuid,
	_validate_webshop_url,
)
from payrexx_integration.payrexx_integration.payrexx.payrexx_client import (
	PayrexxAPIError,
	PayrexxClient,
)

GATEWAY_NAME = "StaticQRGW"
AUTOMATION_USER = "Administrator"
CLIENT_MODULE = "payrexx_integration.payrexx_integration.payrexx.payrexx_client"
QR_CODE_UUID = "08cc4152-993a-434b-937d-933359148ee8"
# The canonical public origin of the site under test, plus the separate origin a
# downstream app publishes through its own `*_public_base_url` key — good_npo
# builds campaign QR targets from `good_npo_public_base_url`, whose precedence
# chain is its own, so the QR target must be validated against the configured
# origin allowlist rather than the scheme alone.
PUBLIC_ORIGIN_CONF = {
	"host_name": "https://ngo.example.test",
	"good_npo_public_base_url": "https://spenden.example.test",
}


def _public_origins(**extra):
	"""Configure the operator-published origins for a QR target validation."""
	return patch.dict(frappe.conf, {**PUBLIC_ORIGIN_CONF, **extra})


def _ensure_settings(automation_user: str = AUTOMATION_USER) -> str:
	"""Create the QR test gateway row (if missing) and return its name.

	``automation_user`` is mandatory since 16.1.9 — every settings-controller
	provider path runs inside ``as_automation_user``, so the fixture must name
	an enabled System User or checkout and QR creation fail closed.
	"""
	if frappe.db.exists("Payrexx Settings", {"gateway_name": GATEWAY_NAME}):
		settings_name = frappe.db.get_value("Payrexx Settings", {"gateway_name": GATEWAY_NAME}, "name")
		if not frappe.db.get_value("Payrexx Settings", settings_name, "automation_user"):
			frappe.db.set_value(
				"Payrexx Settings",
				settings_name,
				"automation_user",
				automation_user,
				update_modified=False,
			)
			frappe.clear_document_cache("Payrexx Settings", settings_name)
		return settings_name
	return (
		frappe.get_doc(
			{
				"doctype": "Payrexx Settings",
				"gateway_name": GATEWAY_NAME,
				"instance_name": "test-instance",
				"api_base_domain": "payrexx.com",
				"api_secret": "sk_test_dummy",
				"webhook_signing_key": "whk_test_dummy",
				"automation_user": automation_user,
				"supported_currencies": "CHF,EUR",
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def _http_error(status_code: int) -> HTTPError:
	response = Mock()
	response.status_code = status_code
	return HTTPError(response=response)


def _provider_response(status_code: int) -> Response:
	response = Response()
	response.status_code = status_code
	response.reason = "Provider status"
	response.url = f"https://api.payrexx.com/v1.14/QrCode/{QR_CODE_UUID}/?instance=demo"
	return response


class _StubProviderSession:
	"""Real request preparation, canned response (mirrors `test_checkout_security`).

	Keeps the client's auth callable on the actual wire path while making the
	provider status deterministic and offline.
	"""

	def __init__(self, response: Response):
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


class TestStaticQrClient(IntegrationTestCase):
	def _client(self) -> PayrexxClient:
		return PayrexxClient(instance="demo", api_secret="sk_test_dummy", api_version="v1.14")

	def test_create_qr_code_posts_webshop_url_and_unwraps(self):
		client = self._client()
		provider_payload = {
			"status": "success",
			"data": [
				{
					"uuid": "08cc4152-993a-434b-937d-933359148ee8",
					"webshopUrl": "https://ngo.example.test/donate-campaign/x",
					"png": "data:image/png;base64,AAAA",
					"svg": "data:image/svg+xml;base64,BBBB",
				}
			],
		}
		with patch(
			"payrexx_integration.payrexx_integration.payrexx.payrexx_client._execute_request",
			return_value=provider_payload,
		) as execute:
			qr_code = client.create_qr_code("https://ngo.example.test/donate-campaign/x")

		self.assertEqual(qr_code["uuid"], "08cc4152-993a-434b-937d-933359148ee8")
		method, url = execute.call_args[0]
		self.assertEqual(method, "POST")
		self.assertEqual(url, "https://api.payrexx.com/v1.14/QrCode/?instance=demo")
		self.assertEqual(
			execute.call_args.kwargs["data"],
			{"webshopUrl": "https://ngo.example.test/donate-campaign/x"},
		)

	def test_create_qr_code_raises_on_provider_error_envelope(self):
		client = self._client()
		with (
			patch(
				"payrexx_integration.payrexx_integration.payrexx.payrexx_client._execute_request",
				return_value={"status": "error", "message": "instance not found"},
			),
			self.assertRaisesRegex(PayrexxAPIError, "instance not found"),
		):
			client.create_qr_code("https://ngo.example.test/donate-campaign/x")

	def test_delete_qr_code_issues_delete_request(self):
		client = self._client()
		with patch(
			"payrexx_integration.payrexx_integration.payrexx.payrexx_client._execute_request",
			return_value={"status": "success", "data": []},
		) as execute:
			client.delete_qr_code("08cc4152-993a-434b-937d-933359148ee8")

		method, url = execute.call_args[0]
		self.assertEqual(method, "DELETE")
		self.assertEqual(
			url,
			"https://api.payrexx.com/v1.14/QrCode/08cc4152-993a-434b-937d-933359148ee8/?instance=demo",
		)


class TestStaticQrValidation(IntegrationTestCase):
	def test_sanitize_qr_session_value_accepts_provider_shapes(self):
		self.assertEqual(_sanitize_qr_session_value(" abc-123 "), "abc-123")
		self.assertEqual(
			_sanitize_qr_session_value("08cc4152-993a-434b-937d-933359148ee8"),
			"08cc4152-993a-434b-937d-933359148ee8",
		)
		self.assertEqual(_sanitize_qr_session_value("ch.twint.payment"), "ch.twint.payment")
		self.assertEqual(_sanitize_qr_session_value("twint-issuer1:"), "twint-issuer1:")

	def test_sanitize_qr_session_value_drops_unsafe_values(self):
		self.assertIsNone(_sanitize_qr_session_value(None))
		self.assertIsNone(_sanitize_qr_session_value(""))
		self.assertIsNone(_sanitize_qr_session_value("has space"))
		self.assertIsNone(_sanitize_qr_session_value("slash/injection"))
		self.assertIsNone(_sanitize_qr_session_value("query&injection=1"))
		self.assertIsNone(_sanitize_qr_session_value("x" * 129))

	def test_validate_webshop_url(self):
		with _public_origins():
			self.assertEqual(
				_validate_webshop_url(" https://ngo.example.test/donate-campaign/x "),
				"https://ngo.example.test/donate-campaign/x",
			)
			for bad in (
				"",
				"/relative/path",
				"ftp://ngo.example.test/x",
				"https://user:pw@ngo.example.test/x",
				"https://ngo.example.test/" + "x" * 1000,
			):
				with self.subTest(url=bad), self.assertRaises(frappe.ValidationError):
					_validate_webshop_url(bad)

	def test_validate_webshop_url_accepts_configured_public_base_origin(self):
		"""A campaign QR target good_npo built from its own public base must pass.

		good_npo resolves `good_npo_public_base_url` -> `good_demo_public_base_url`
		-> `host_name`, a different precedence chain than this app's
		`get_public_url`, so its legitimate URL is frequently *not* the canonical
		host_name origin.
		"""
		with _public_origins():
			self.assertEqual(
				_validate_webshop_url("https://spenden.example.test/donate-campaign?campaign=NPO-CAMP-0001"),
				"https://spenden.example.test/donate-campaign?campaign=NPO-CAMP-0001",
			)

	def test_validate_webshop_url_rejects_foreign_origin(self):
		"""A stale or mistyped public base must not mint a permanent printed QR."""
		with _public_origins():
			for foreign in (
				"https://evil.example.org/donate-campaign?campaign=NPO-CAMP-0001",
				# Same host, but neither the configured scheme nor the effective port.
				"http://spenden.example.test/donate-campaign",
				"https://spenden.example.test:8443/donate-campaign",
			):
				with self.subTest(url=foreign), self.assertRaises(frappe.ValidationError):
					_validate_webshop_url(foreign)

	def test_validate_qr_code_uuid(self):
		self.assertEqual(
			_validate_qr_code_uuid("08cc4152-993a-434b-937d-933359148ee8"),
			"08cc4152-993a-434b-937d-933359148ee8",
		)
		for bad in ("", "short", "has space in it", "path/../traversal", "x" * 65):
			with self.assertRaises(frappe.ValidationError):
				_validate_qr_code_uuid(bad)


class TestStaticQrSettings(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.settings_name = _ensure_settings()

	def _settings(self):
		return frappe.get_doc("Payrexx Settings", self.settings_name)

	def test_create_static_qr_returns_provider_payload(self):
		settings = self._settings()
		client = Mock()
		client.create_qr_code.return_value = {
			"uuid": QR_CODE_UUID,
			"webshopUrl": "https://ngo.example.test/donate-campaign/x",
			"png": "data:image/png;base64,AAAA",
			"svg": "data:image/svg+xml;base64,BBBB",
		}
		with _public_origins(), patch.object(type(settings), "_client", return_value=client):
			qr_code = settings.create_static_qr("https://ngo.example.test/donate-campaign/x")

		self.assertEqual(qr_code["uuid"], QR_CODE_UUID)
		client.create_qr_code.assert_called_once_with("https://ngo.example.test/donate-campaign/x")

	def test_create_static_qr_accepts_a_configured_public_base_url_target(self):
		"""The upstream good_npo campaign QR path must keep working end to end."""
		settings = self._settings()
		donate_url = "https://spenden.example.test/donate-campaign?campaign=NPO-CAMP-0001"
		client = Mock()
		client.create_qr_code.return_value = {
			"uuid": QR_CODE_UUID,
			"webshopUrl": donate_url,
			"png": "data:image/png;base64,AAAA",
			"svg": "data:image/svg+xml;base64,BBBB",
		}
		with _public_origins(), patch.object(type(settings), "_client", return_value=client):
			qr_code = settings.create_static_qr(donate_url)

		self.assertEqual(qr_code["uuid"], QR_CODE_UUID)
		client.create_qr_code.assert_called_once_with(donate_url)

	def test_create_static_qr_rejects_foreign_origin_before_provider_contact(self):
		settings = self._settings()
		client = Mock()
		with (
			_public_origins(),
			patch.object(type(settings), "_client", return_value=client),
			self.assertRaises(frappe.ValidationError),
		):
			settings.create_static_qr("https://evil.example.org/donate-campaign?campaign=NPO-CAMP-0001")
		client.create_qr_code.assert_not_called()

	def test_static_qr_provider_calls_run_as_the_owning_automation_user(self):
		"""QR creation is a settings-controller provider path — same user contract."""
		user_name = f"payrexx-qr-{frappe.generate_hash(length=10)}@example.test"
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": user_name,
				"first_name": "Payrexx QR",
				"enabled": 1,
				"user_type": "System User",
				"send_welcome_email": 0,
			}
		)
		user.append("roles", {"role": "System Manager"})
		user.insert(ignore_permissions=True)

		# The in-memory row is enough: as_automation_user reads the Document it is
		# handed, so the shared fixture row is left untouched.
		settings = self._settings()
		settings.automation_user = user_name
		previous_user = frappe.session.user

		observed = []
		client = Mock()
		client.create_qr_code.side_effect = lambda _url: (
			observed.append(frappe.session.user) or {"uuid": QR_CODE_UUID}
		)
		with _public_origins(), patch.object(type(settings), "_client", return_value=client):
			settings.create_static_qr("https://ngo.example.test/donate-campaign/x")

		self.assertEqual(observed, [user_name])
		self.assertEqual(frappe.session.user, previous_user)

	def test_create_static_qr_rejects_invalid_url_before_provider_contact(self):
		settings = self._settings()
		client = Mock()
		with (
			patch.object(type(settings), "_client", return_value=client),
			self.assertRaises(frappe.ValidationError),
		):
			settings.create_static_qr("javascript:alert(1)")
		client.create_qr_code.assert_not_called()

	def test_create_static_qr_rejects_incomplete_provider_payload(self):
		settings = self._settings()
		client = Mock()
		client.create_qr_code.return_value = {"webshopUrl": "https://ngo.example.test/x"}
		with (
			_public_origins(),
			patch.object(type(settings), "_client", return_value=client),
			self.assertRaisesRegex(frappe.ValidationError, "incomplete"),
		):
			settings.create_static_qr("https://ngo.example.test/donate-campaign/x")

	def test_delete_static_qr_tolerates_provider_404(self):
		settings = self._settings()
		client = Mock()
		client.delete_qr_code.side_effect = _http_error(404)
		with patch.object(type(settings), "_client", return_value=client):
			settings.delete_static_qr(QR_CODE_UUID)
		# The tolerated status is declared to the client so it logs no Error Log row.
		client.delete_qr_code.assert_called_once_with(
			QR_CODE_UUID, expected_statuses=QR_DELETE_TOLERATED_STATUSES
		)

	def test_delete_static_qr_raises_on_other_provider_errors(self):
		settings = self._settings()
		client = Mock()
		client.delete_qr_code.side_effect = _http_error(500)
		with (
			patch.object(type(settings), "_client", return_value=client),
			self.assertRaises(frappe.ValidationError),
		):
			settings.delete_static_qr(QR_CODE_UUID)


class TestStaticQrProviderErrorLogging(IntegrationTestCase):
	"""Audit finding F11c: a tolerated provider status must not create error noise.

	``delete_static_qr`` treats a provider 404 as "already deleted" and returns
	cleanly, so the request path must not have written an Error Log row for staff
	to triage on the way there. Every undeclared status keeps logging exactly as
	before.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.settings_name = _ensure_settings()

	def _settings(self):
		return frappe.get_doc("Payrexx Settings", self.settings_name)

	def _error_logs_for(self, status_code: int, provider_call, *, raises=None) -> list:
		"""Run one real client request against a canned status, capturing Error Logs."""
		logged = []
		client = PayrexxClient(instance="demo", api_secret="sk_test_dummy")
		session = _StubProviderSession(_provider_response(status_code))
		# An Integration Request doc on the flag would take the other logging branch.
		original_request_doc = frappe.flags.integration_request_doc
		frappe.flags.integration_request_doc = None
		try:
			with (
				patch(f"{CLIENT_MODULE}.get_request_session", return_value=session),
				patch.object(frappe, "log_error", side_effect=lambda *a, **kw: logged.append(kw or a)),
				self.assertRaises(raises) if raises else nullcontext(),
			):
				provider_call(client)
		finally:
			frappe.flags.integration_request_doc = original_request_doc
		return logged

	def test_tolerated_delete_404_writes_no_error_log(self):
		settings = self._settings()

		def delete(client):
			with patch.object(type(settings), "_client", return_value=client):
				settings.delete_static_qr(QR_CODE_UUID)

		self.assertEqual(self._error_logs_for(404, delete), [])

	def test_unexpected_delete_500_still_writes_an_error_log(self):
		settings = self._settings()

		def delete(client):
			with patch.object(type(settings), "_client", return_value=client):
				settings.delete_static_qr(QR_CODE_UUID)

		self.assertTrue(self._error_logs_for(500, delete, raises=frappe.ValidationError))

	def test_client_logs_an_undeclared_status_by_default(self):
		"""Backwards compatibility: without a declaration nothing changes."""
		logged = self._error_logs_for(
			404,
			lambda client: client.delete_qr_code(QR_CODE_UUID),
			raises=HTTPError,
		)
		self.assertTrue(logged)


class TestQrSessionCheckoutPassthrough(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.settings_name = _ensure_settings()

	def _settings(self):
		return frappe.get_doc("Payrexx Settings", self.settings_name)

	def test_gateway_payload_includes_sanitized_qr_session(self):
		payload = self._settings()._build_create_gateway_payload(
			{
				"amount": 50,
				"currency": "CHF",
				"description": "Donation",
				"qr_code_session_id": "session-123",
				"return_app": "ch.twint.payment",
			},
			"PAYREXX-IR-TEST",
		)
		self.assertEqual(payload["qrCodeSessionId"], "session-123")
		self.assertEqual(payload["returnApp"], "ch.twint.payment")

	def test_gateway_payload_drops_invalid_session_values_silently(self):
		payload = self._settings()._build_create_gateway_payload(
			{
				"amount": 50,
				"currency": "CHF",
				"qr_code_session_id": "bad session/value",
				"return_app": "ch.twint.payment",
			},
			"PAYREXX-IR-TEST",
		)
		self.assertNotIn("qrCodeSessionId", payload)
		# Without a session id the return app is meaningless — never sent alone.
		self.assertNotIn("returnApp", payload)

	def test_gateway_payload_omits_return_app_without_value(self):
		payload = self._settings()._build_create_gateway_payload(
			{
				"amount": 50,
				"currency": "CHF",
				"qr_code_session_id": "session-123",
				"return_app": "bad value with spaces",
			},
			"PAYREXX-IR-TEST",
		)
		self.assertEqual(payload["qrCodeSessionId"], "session-123")
		self.assertNotIn("returnApp", payload)

	def _reference_todo(self) -> str:
		return (
			frappe.get_doc({"doctype": "ToDo", "description": "Payrexx static QR test reference"})
			.insert(ignore_permissions=True)
			.name
		)

	def test_get_payment_url_prefers_app_link_for_qr_session_checkout(self):
		settings = self._settings()
		reference_name = self._reference_todo()
		client = Mock()
		client.create_gateway.return_value = {
			"id": 4243,
			"hash": "h",
			"link": "https://pay.example/checkout",
			"appLink": "twint-issuer1://payment?session=abc",
		}
		with (
			patch.object(type(settings), "_client", return_value=client),
			patch.object(type(settings), "_validate_payment_request_source", return_value=None),
		):
			url = settings.get_payment_url(
				amount=25,
				currency="CHF",
				reference_doctype="ToDo",
				reference_docname=reference_name,
				qr_code_session_id="session-123",
				return_app="ch.twint.payment",
			)

		self.assertEqual(url, "twint-issuer1://payment?session=abc")
		payload = client.create_gateway.call_args[0][0]
		self.assertEqual(payload["qrCodeSessionId"], "session-123")
		self.assertEqual(payload["returnApp"], "ch.twint.payment")

		ir_name = frappe.get_all(
			"Integration Request",
			filters={"integration_request_service": "Payrexx", "reference_docname": reference_name},
			order_by="creation desc",
			limit=1,
			pluck="name",
		)[0]
		data = frappe.parse_json(frappe.db.get_value("Integration Request", ir_name, "data"))
		# The hosted checkout URL stays canonical; the app link is recorded alongside.
		self.assertEqual(data.get("payrexx_checkout_url"), "https://pay.example/checkout")
		self.assertEqual(data.get("payrexx_gateway_app_link"), "twint-issuer1://payment?session=abc")

	def test_get_payment_url_returns_hosted_link_without_qr_session(self):
		settings = self._settings()
		client = Mock()
		client.create_gateway.return_value = {
			"id": 4244,
			"hash": "h",
			"link": "https://pay.example/checkout",
			"appLink": "twint-issuer1://payment?session=abc",
		}
		with (
			patch.object(type(settings), "_client", return_value=client),
			patch.object(type(settings), "_validate_payment_request_source", return_value=None),
		):
			url = settings.get_payment_url(
				amount=25,
				currency="CHF",
				reference_doctype="ToDo",
				reference_docname=self._reference_todo(),
			)

		self.assertEqual(url, "https://pay.example/checkout")
