# Copyright (c) 2026, Goodvantage GmbH and contributors
# See license.txt

from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase
from requests import HTTPError

from payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings import (
	_sanitize_qr_session_value,
	_validate_qr_code_uuid,
	_validate_webshop_url,
)
from payrexx_integration.payrexx_integration.payrexx.payrexx_client import (
	PayrexxAPIError,
	PayrexxClient,
)

GATEWAY_NAME = "StaticQRGW"


def _ensure_settings() -> str:
	if frappe.db.exists("Payrexx Settings", {"gateway_name": GATEWAY_NAME}):
		return frappe.db.get_value("Payrexx Settings", {"gateway_name": GATEWAY_NAME}, "name")
	return (
		frappe.get_doc(
			{
				"doctype": "Payrexx Settings",
				"gateway_name": GATEWAY_NAME,
				"instance_name": "test-instance",
				"api_base_domain": "payrexx.com",
				"api_secret": "sk_test_dummy",
				"webhook_signing_key": "whk_test_dummy",
				"api_version": "v1.14",
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
			with self.assertRaises(frappe.ValidationError):
				_validate_webshop_url(bad)

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
			"uuid": "08cc4152-993a-434b-937d-933359148ee8",
			"webshopUrl": "https://ngo.example.test/donate-campaign/x",
			"png": "data:image/png;base64,AAAA",
			"svg": "data:image/svg+xml;base64,BBBB",
		}
		with patch.object(type(settings), "_client", return_value=client):
			qr_code = settings.create_static_qr("https://ngo.example.test/donate-campaign/x")

		self.assertEqual(qr_code["uuid"], "08cc4152-993a-434b-937d-933359148ee8")
		client.create_qr_code.assert_called_once_with("https://ngo.example.test/donate-campaign/x")

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
			patch.object(type(settings), "_client", return_value=client),
			self.assertRaisesRegex(frappe.ValidationError, "incomplete"),
		):
			settings.create_static_qr("https://ngo.example.test/donate-campaign/x")

	def test_delete_static_qr_tolerates_provider_404(self):
		settings = self._settings()
		client = Mock()
		client.delete_qr_code.side_effect = _http_error(404)
		with patch.object(type(settings), "_client", return_value=client):
			settings.delete_static_qr("08cc4152-993a-434b-937d-933359148ee8")
		client.delete_qr_code.assert_called_once()

	def test_delete_static_qr_raises_on_other_provider_errors(self):
		settings = self._settings()
		client = Mock()
		client.delete_qr_code.side_effect = _http_error(500)
		with (
			patch.object(type(settings), "_client", return_value=client),
			self.assertRaises(frappe.ValidationError),
		):
			settings.delete_static_qr("08cc4152-993a-434b-937d-933359148ee8")


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
