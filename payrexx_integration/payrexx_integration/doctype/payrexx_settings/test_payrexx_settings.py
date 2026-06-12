# Copyright (c) 2026, Goodvantage GmbH and contributors
# See license.txt

import base64
import hashlib
import hmac
import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import frappe
from frappe.tests import IntegrationTestCase
from requests import HTTPError
from requests.models import Response

from payrexx_integration.api import _sign, payment_success, payrexx_pay_url
from payrexx_integration.payrexx_integration.payrexx.payrexx_client import PayrexxClient
from payrexx_integration.payrexx_integration.payrexx.webhook_validator import (
	verify_webhook_signature,
)

GATEWAY_NAME = "TestGW"
SETTINGS_NAME_PREFIX = "Payrexx-Test-"


def _ensure_settings(name: str = GATEWAY_NAME) -> str:
	"""Create a Payrexx Settings row (if missing) and return its name."""
	if frappe.db.exists("Payrexx Settings", {"gateway_name": name}):
		return frappe.db.get_value("Payrexx Settings", {"gateway_name": name}, "name")

	doc = frappe.get_doc(
		{
			"doctype": "Payrexx Settings",
			"gateway_name": name,
			"instance_name": "test-instance",
			"api_base_domain": "payrexx.com",
			"api_secret": "sk_test_dummy",
			"webhook_signing_key": "whk_test_dummy",
			"api_version": "v1.14",
			"supported_currencies": "CHF,EUR,USD",
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


class TestPayrexxSettings(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.settings_name = _ensure_settings()

	# ----------------------------------------------------- on_update registers

	def test_save_creates_payment_gateway_row(self):
		gateway = "Payrexx-" + GATEWAY_NAME
		self.assertTrue(
			frappe.db.exists("Payment Gateway", gateway),
			f"Payment Gateway row {gateway!r} should exist after Payrexx Settings save",
		)
		row = frappe.get_doc("Payment Gateway", gateway)
		self.assertEqual(row.gateway_settings, "Payrexx Settings")
		self.assertEqual(row.gateway_controller, GATEWAY_NAME)

	# ----------------------------------------------------- currency validator

	def test_validate_transaction_currency_accepts_supported(self):
		doc = frappe.get_doc("Payrexx Settings", self.settings_name)
		# Should not raise
		doc.validate_transaction_currency("CHF")

	def test_validate_transaction_currency_rejects_unsupported(self):
		doc = frappe.get_doc("Payrexx Settings", self.settings_name)
		with self.assertRaises(frappe.ValidationError):
			doc.validate_transaction_currency("XYZ")

	# ----------------------------------------------------- HMAC pay-link token

	def test_pay_url_token_round_trip(self):
		url = payrexx_pay_url("ACC-SINV-2026-00001", gateway_name=self.settings_name)
		params = parse_qs(urlparse(url).query)
		self.assertEqual(params.get("si"), ["ACC-SINV-2026-00001"])
		self.assertEqual(params.get("gateway_name"), [self.settings_name])
		token = params["token"][0]
		self.assertEqual(len(token), 32)
		# Tampering with the invoice name must invalidate the token.
		self.assertNotEqual(token, _sign("ACC-SINV-2026-00002", self.settings_name))
		# Tampering with the gateway must also invalidate links generated with gateway_name.
		self.assertNotEqual(token, _sign("ACC-SINV-2026-00001", "OtherGateway"))

	def test_pay_url_uses_configured_public_host_without_dev_port(self):
		from payrexx_integration.url_utils import get_public_url

		original_host_name = frappe.conf.get("host_name")
		try:
			frappe.conf.host_name = "https://demo.example.test"
			self.assertEqual(get_public_url("/demo?x=1"), "https://demo.example.test/demo?x=1")
		finally:
			if original_host_name is None:
				frappe.conf.pop("host_name", None)
			else:
				frappe.conf.host_name = original_host_name

	def test_webhook_url_uses_configured_public_host_without_dev_port(self):
		from payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings import (
			get_webhook_url,
		)

		original_host_name = frappe.conf.get("host_name")
		try:
			frappe.conf.host_name = "https://demo.example.test"
			url = get_webhook_url("Sandbox")
			parts = urlparse(url)
			self.assertEqual(parts.scheme, "https")
			self.assertEqual(parts.netloc, "demo.example.test")
			self.assertEqual(
				parts.path,
				"/api/method/payrexx_integration.payrexx_integration.doctype."
				"payrexx_settings.payrexx_settings.callback",
			)
			self.assertEqual(parse_qs(parts.query).get("gateway_name"), ["Sandbox"])
		finally:
			if original_host_name is None:
				frappe.conf.pop("host_name", None)
			else:
				frappe.conf.host_name = original_host_name

	def test_payrexx_client_uses_default_api_domain(self):
		client = PayrexxClient(instance="demo", api_secret="sk_test_dummy", api_version="v1.14")
		self.assertEqual(
			client._url("Gateway/"),
			"https://api.payrexx.com/v1.14/Gateway/?instance=demo",
		)

	def test_payrexx_client_uses_platform_api_domain(self):
		client = PayrexxClient(
			instance="customer",
			api_secret="sk_test_dummy",
			api_version="v1.14",
			api_base_domain="pay.goodvantage.ch",
		)
		self.assertEqual(
			client._url("Gateway/"),
			"https://api.pay.goodvantage.ch/v1.14/Gateway/?instance=customer",
		)

	def test_settings_client_passes_platform_api_domain(self):
		doc = frappe.get_doc("Payrexx Settings", self.settings_name)
		doc.instance_name = "customer"
		doc.api_base_domain = "pay.goodvantage.ch"
		client = doc._client()
		self.assertEqual(client.instance, "customer")
		self.assertEqual(client.api_base_domain, "pay.goodvantage.ch")
		self.assertEqual(
			client._url("Gateway/0/"),
			"https://api.pay.goodvantage.ch/v1.14/Gateway/0/?instance=customer",
		)

	def test_settings_ping_uses_client(self):
		doc = frappe.get_doc("Payrexx Settings", self.settings_name)

		class _FakeClient:
			instance = "test-instance"
			api_base_domain = "payrexx.com"

			def ping_gateway(self) -> dict:
				return {"status": "error", "message": "No Gateway found with id 0"}

		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		with patch.object(ps_module.PayrexxSettings, "_client", return_value=_FakeClient()):
			doc._ping()

	def test_settings_ping_rejects_http_auth_error(self):
		doc = frappe.get_doc("Payrexx Settings", self.settings_name)
		response = Response()
		response.status_code = 403

		class _FakeClient:
			instance = "test-instance"
			api_base_domain = "payrexx.com"

			def ping_gateway(self) -> dict:
				raise HTTPError(response=response)

		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		with (
			patch.object(ps_module.PayrexxSettings, "_client", return_value=_FakeClient()),
			self.assertRaises(frappe.ValidationError) as exc,
		):
			doc._ping()
		self.assertIn("Payrexx rejected the API Secret", str(exc.exception))

	def test_payrexx_client_falls_back_to_default_api_domain_on_custom_auth_reject(self):
		client = PayrexxClient(
			instance="customer",
			api_secret="sk_test_dummy",
			api_version="v1.14",
			api_base_domain="pay.goodvantage.ch",
		)
		called_urls = []

		def fake_post_request(url, **kwargs):
			called_urls.append(url)
			if "api.pay.goodvantage.ch" in url:
				response = Response()
				response.status_code = 403
				response.url = url
				raise HTTPError(response=response)
			return {"status": "success", "data": [{"id": 123, "link": "https://pay.example"}]}

		with patch(
			"payrexx_integration.payrexx_integration.payrexx.payrexx_client.make_post_request",
			side_effect=fake_post_request,
		):
			gateway = client.create_gateway({"amount": 100})

		self.assertEqual(gateway["link"], "https://pay.example")
		self.assertEqual(
			called_urls,
			[
				"https://api.pay.goodvantage.ch/v1.14/Gateway/?instance=customer",
				"https://api.payrexx.com/v1.14/Gateway/?instance=customer",
			],
		)

	def test_gateway_payload_uses_per_checkout_failure_return_url(self):
		doc = frappe.get_doc("Payrexx Settings", self.settings_name)
		original_host_name = frappe.conf.get("host_name")
		try:
			frappe.conf.host_name = "https://demo.example.test"
			payload = doc._build_create_gateway_payload(
				{
					"amount": 50,
					"currency": "CHF",
					"description": "Demo donation",
					"reference_doctype": "Donation",
					"reference_docname": "NPO-DTN-TEST",
					"failed_redirect_to": "/demo?donation_status=failed&donation=NPO-DTN-TEST",
					"cancel_redirect_to": "/demo?donation_status=failed&donation=NPO-DTN-TEST",
				},
				"PAYREXX-IR-TEST",
			)
		finally:
			if original_host_name is None:
				frappe.conf.pop("host_name", None)
			else:
				frappe.conf.host_name = original_host_name

		self.assertEqual(
			payload["failedRedirectUrl"],
			"https://demo.example.test/demo?donation_status=failed&donation=NPO-DTN-TEST",
		)
		self.assertEqual(payload["cancelRedirectUrl"], payload["failedRedirectUrl"])

	def test_pay_url_explicit_gateway_name(self):
		other_settings = _ensure_settings("OtherGateway")
		url = payrexx_pay_url("ACC-SINV-2026-00001", gateway_name=other_settings)
		params = parse_qs(urlparse(url).query)
		self.assertEqual(params.get("gateway_name"), [other_settings])
		self.assertEqual(params.get("token"), [_sign("ACC-SINV-2026-00001", other_settings)])

	def test_pay_url_blank_invoice_returns_blank(self):
		self.assertEqual(payrexx_pay_url(None), "")
		self.assertEqual(payrexx_pay_url(""), "")

	# ----------------------------------------------------- webhook signature

	def test_webhook_signature_base64(self):
		key = "whk_test_dummy"
		body = b'{"transaction":{"id":1,"status":"confirmed"}}'
		sig = base64.b64encode(hmac.new(key.encode("utf-8"), body, hashlib.sha256).digest()).decode("ascii")
		self.assertTrue(verify_webhook_signature(body, sig, key))

	def test_webhook_signature_hex_fallback(self):
		key = "whk_test_dummy"
		body = b'{"transaction":{"id":1,"status":"confirmed"}}'
		sig_hex = hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()
		self.assertTrue(verify_webhook_signature(body, sig_hex, key))

	def test_webhook_signature_rejects_tampered(self):
		key = "whk_test_dummy"
		good_body = b'{"transaction":{"id":1,"status":"confirmed"}}'
		bad_body = b'{"transaction":{"id":1,"status":"refunded"}}'
		sig = base64.b64encode(hmac.new(key.encode("utf-8"), good_body, hashlib.sha256).digest()).decode(
			"ascii"
		)
		self.assertFalse(verify_webhook_signature(bad_body, sig, key))
		self.assertFalse(verify_webhook_signature(good_body, "", key))
		self.assertFalse(verify_webhook_signature(good_body, sig, ""))

	# ----------------------------------------------------- redirect endpoint

	def test_pay_invoice_rejects_bad_token(self):
		from payrexx_integration.api import pay_invoice

		with self.assertRaises(frappe.PermissionError):
			pay_invoice(si="ACC-SINV-2026-00001", token="badtoken")

	def test_pay_invoice_rejects_missing_invoice(self):
		from payrexx_integration.api import pay_invoice

		fake_name = "ACC-SINV-DOES-NOT-EXIST"
		with self.assertRaises(frappe.DoesNotExistError):
			pay_invoice(si=fake_name, token=_sign(fake_name))

	# ---------------------------------------------------- callback (full path)

	def test_callback_marks_integration_request_completed(self):
		# Set up an Integration Request the callback should resolve to
		ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Queued",
				"data": json.dumps({"payrexx_gateway_id": 999}),
			}
		).insert(ignore_permissions=True)

		body = json.dumps(
			{
				"transaction": {
					"id": 12345,
					"status": "confirmed",
					"referenceId": ir.name,
					"invoice": {"referenceId": ir.name},
				}
			}
		).encode("utf-8")
		sig = base64.b64encode(hmac.new(b"whk_test_dummy", body, hashlib.sha256).digest()).decode("ascii")

		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		# Patch frappe.request just for this call so callback() can read body+headers.
		class _FakeRequest:
			def __init__(self):
				self.args = {}
				self.form = {}

			def get_data(self):
				return body

		original_request = getattr(frappe.local, "request", None)
		original_header = frappe.get_request_header
		frappe.local.request = _FakeRequest()
		frappe.get_request_header = lambda name, default="": (  # type: ignore[assignment]
			sig if name == "X-Webhook-Signature" else default
		)
		try:
			ps_module.callback(gateway_name=GATEWAY_NAME)
		finally:
			frappe.get_request_header = original_header  # type: ignore[assignment]
			if original_request is None:
				delattr(frappe.local, "request")
			else:
				frappe.local.request = original_request

		ir.reload()
		self.assertEqual(ir.status, "Completed")

	def test_callback_ignores_non_payrexx_integration_request(self):
		ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "OtherGateway",
				"status": "Queued",
				"data": "{}",
			}
		).insert(ignore_permissions=True)

		body = json.dumps(
			{
				"transaction": {
					"id": 12345,
					"status": "confirmed",
					"referenceId": ir.name,
					"invoice": {"referenceId": ir.name},
				}
			}
		).encode("utf-8")
		sig = base64.b64encode(hmac.new(b"whk_test_dummy", body, hashlib.sha256).digest()).decode("ascii")

		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		class _FakeRequest:
			def __init__(self):
				self.args = {}
				self.form = {}

			def get_data(self):
				return body

		original_request = getattr(frappe.local, "request", None)
		original_header = frappe.get_request_header
		frappe.local.request = _FakeRequest()
		frappe.get_request_header = lambda name, default="": (  # type: ignore[assignment]
			sig if name == "X-Webhook-Signature" else default
		)
		try:
			with patch("frappe.log_error") as log_error:
				self.assertEqual(ps_module.callback(gateway_name=GATEWAY_NAME), {"ok": True})
				log_error.assert_called_once()
		finally:
			frappe.get_request_header = original_header  # type: ignore[assignment]
			if original_request is None:
				delattr(frappe.local, "request")
			else:
				frappe.local.request = original_request

		ir.reload()
		self.assertEqual(ir.status, "Queued")

	def test_get_payment_url_records_owning_settings_on_integration_request(self):
		"""The settings row that creates a checkout is recorded on the IR —
		the webhook binding depends on this."""
		settings = frappe.get_doc("Payrexx Settings", self.settings_name)

		class _FakeClient:
			def create_gateway(self, payload):
				return {"id": 4242, "hash": "h", "link": "https://pay.example/checkout"}

		with patch.object(type(settings), "_client", return_value=_FakeClient()):
			# No reference document: create_request_log link-validates it,
			# and the recording behavior under test is independent of it.
			link = settings.get_payment_url(
				amount=10,
				currency="CHF",
				payment_gateway="Payrexx-" + self.settings_name,
			)
		self.assertEqual(link, "https://pay.example/checkout")

		ir_name = frappe.get_all(
			"Integration Request",
			filters={"integration_request_service": "Payrexx"},
			order_by="creation desc",
			limit=1,
			pluck="name",
		)[0]
		data = frappe.parse_json(frappe.db.get_value("Integration Request", ir_name, "data"))
		self.assertEqual(data.get("payrexx_settings"), self.settings_name)
		self.assertEqual(data.get("payrexx_gateway_id"), 4242)

	def test_reconcile_prefers_integration_requests_own_gateway(self):
		"""The caller-supplied gateway_name must not pick the credentials —
		the IR's stored gateway does."""
		other_settings = _ensure_settings("OtherGateway")
		ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Queued",
				"data": json.dumps({"payrexx_gateway_id": 777, "payrexx_settings": other_settings}),
			}
		).insert(ignore_permissions=True)

		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		resolved = []
		original_resolve = ps_module._resolve_settings

		def capture_resolve(gateway_name):
			resolved.append(gateway_name)
			settings = original_resolve(gateway_name)

			class _FakeClient:
				def retrieve_gateway(self, gateway_id):
					return {"status": "waiting", "invoices": []}

			settings._client = lambda: _FakeClient()
			return settings

		with patch.object(ps_module, "_resolve_settings", side_effect=capture_resolve):
			ps_module.reconcile_integration_request(ir.name, gateway_name=GATEWAY_NAME)

		self.assertEqual(resolved, [other_settings])

	def test_callback_rejects_gateway_mismatch(self):
		"""A webhook verified with one gateway's key must not complete another gateway's request."""
		other_settings = _ensure_settings("OtherGateway")
		ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Queued",
				"data": json.dumps({"payrexx_gateway_id": 999, "payrexx_settings": other_settings}),
			}
		).insert(ignore_permissions=True)

		body = json.dumps(
			{
				"transaction": {
					"id": 12345,
					"status": "confirmed",
					"referenceId": ir.name,
					"invoice": {"referenceId": ir.name},
				}
			}
		).encode("utf-8")
		# Signature is valid for GATEWAY_NAME's signing key, but the Integration
		# Request belongs to OtherGateway — the callback must refuse to touch it.
		sig = base64.b64encode(hmac.new(b"whk_test_dummy", body, hashlib.sha256).digest()).decode("ascii")

		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		class _FakeRequest:
			def __init__(self):
				self.args = {}
				self.form = {}

			def get_data(self):
				return body

		original_request = getattr(frappe.local, "request", None)
		original_header = frappe.get_request_header
		frappe.local.request = _FakeRequest()
		frappe.get_request_header = lambda name, default="": (  # type: ignore[assignment]
			sig if name == "X-Webhook-Signature" else default
		)
		try:
			with patch("frappe.log_error") as log_error:
				self.assertEqual(ps_module.callback(gateway_name=GATEWAY_NAME), {"ok": True})
				log_error.assert_called_once()
		finally:
			frappe.get_request_header = original_header  # type: ignore[assignment]
			if original_request is None:
				delattr(frappe.local, "request")
			else:
				frappe.local.request = original_request

		ir.reload()
		self.assertEqual(ir.status, "Queued", "Mismatched-gateway webhook must not complete the request")

	def test_callback_reads_gateway_name_from_query_args_for_json_webhook(self):
		_ensure_settings("OtherGateway")
		ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Queued",
				"data": json.dumps({"payrexx_gateway_id": 999}),
			}
		).insert(ignore_permissions=True)

		body = json.dumps(
			{
				"transaction": {
					"id": 12345,
					"status": "confirmed",
					"referenceId": ir.name,
					"invoice": {"referenceId": ir.name},
				}
			}
		).encode("utf-8")
		sig = base64.b64encode(hmac.new(b"whk_test_dummy", body, hashlib.sha256).digest()).decode("ascii")

		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		class _FakeRequest:
			def __init__(self):
				self.args = {"gateway_name": GATEWAY_NAME}
				self.form = {}

			def get_data(self):
				return body

		original_request = getattr(frappe.local, "request", None)
		original_header = frappe.get_request_header
		frappe.local.request = _FakeRequest()
		frappe.get_request_header = lambda name, default="": (  # type: ignore[assignment]
			sig if name == "X-Webhook-Signature" else default
		)
		try:
			ps_module.callback()
		finally:
			frappe.get_request_header = original_header  # type: ignore[assignment]
			if original_request is None:
				delattr(frappe.local, "request")
			else:
				frappe.local.request = original_request

		ir.reload()
		self.assertEqual(ir.status, "Completed")

	def test_payment_authorized_retries_transient_deadlock(self):
		ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Completed",
				"data": "{}",
			}
		).insert(ignore_permissions=True)

		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		calls = []

		def fake_payment_authorized(integration_request, status):
			calls.append((integration_request.name, status))
			if len(calls) == 1:
				raise frappe.QueryDeadlockError((1020, "Record has changed since last read"))

		with (
			patch.object(ps_module, "_on_payment_authorized", side_effect=fake_payment_authorized),
			patch.object(ps_module.frappe.db, "rollback") as rollback,
			patch.object(ps_module.time, "sleep") as sleep,
		):
			ps_module._run_payment_authorized_with_retries(ir.name, "Completed")

		self.assertEqual(calls, [(ir.name, "Completed"), (ir.name, "Completed")])
		rollback.assert_called_once()
		sleep.assert_called_once_with(0.25)

	def test_success_reconciliation_marks_integration_request_completed(self):
		ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Queued",
				"data": json.dumps(
					{
						"payrexx_gateway_id": 999,
						"payment_gateway": "Payrexx-" + GATEWAY_NAME,
					}
				),
			}
		).insert(ignore_permissions=True)

		class _FakeClient:
			def retrieve_gateway(self, gateway_id: int) -> dict:
				self.gateway_id = gateway_id
				return {
					"id": gateway_id,
					"status": "confirmed",
					"invoices": [
						{
							"transactions": [
								{
									"id": 12345,
									"status": "confirmed",
									"referenceId": ir.name,
								}
							]
						}
					],
				}

		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		with patch.object(ps_module.PayrexxSettings, "_client", return_value=_FakeClient()):
			self.assertTrue(ps_module.reconcile_integration_request(ir.name))

		ir.reload()
		self.assertEqual(ir.status, "Completed")
		self.assertEqual((frappe.parse_json(ir.data) or {})["payrexx_transaction"]["id"], 12345)

	def test_payment_success_redirects_directly_to_custom_return_url(self):
		return_url = "https://demo.example.test/demo?donation_status=success&donation=NPO-DTN#donate"
		ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Completed",
				"data": json.dumps({"redirect_to": return_url}),
			}
		).insert(ignore_permissions=True)

		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		original_host_name = frappe.conf.get("host_name")
		original_response = getattr(frappe.local, "response", None)
		try:
			frappe.conf.host_name = "https://demo.example.test"
			frappe.local.response = {}
			with patch.object(ps_module, "reconcile_integration_request", return_value=True):
				payment_success(ir=ir.name, gateway_name=GATEWAY_NAME)
			response = dict(frappe.local.response)
		finally:
			if original_host_name is None:
				frappe.conf.pop("host_name", None)
			else:
				frappe.conf.host_name = original_host_name
			if original_response is None:
				frappe.local.response = {}
			else:
				frappe.local.response = original_response

		self.assertEqual(response["type"], "redirect")
		self.assertEqual(response["location"], return_url)

	def test_payment_success_redirects_to_failed_page_when_not_confirmed(self):
		ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Queued",
				"data": json.dumps({"reference_doctype": "Donation", "reference_docname": "NPO-DTN-PENDING"}),
			}
		).insert(ignore_permissions=True)

		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		original_host_name = frappe.conf.get("host_name")
		original_response = getattr(frappe.local, "response", None)
		try:
			frappe.conf.host_name = "https://demo.example.test"
			frappe.local.response = {}
			with patch.object(ps_module, "reconcile_integration_request", return_value=False):
				payment_success(ir=ir.name, gateway_name=GATEWAY_NAME)
			response = dict(frappe.local.response)
		finally:
			if original_host_name is None:
				frappe.conf.pop("host_name", None)
			else:
				frappe.conf.host_name = original_host_name
			if original_response is None:
				frappe.local.response = {}
			else:
				frappe.local.response = original_response

		self.assertEqual(response["type"], "redirect")
		self.assertEqual(
			response["location"],
			"https://demo.example.test/payment-failed?doctype=Donation&docname=NPO-DTN-PENDING",
		)
