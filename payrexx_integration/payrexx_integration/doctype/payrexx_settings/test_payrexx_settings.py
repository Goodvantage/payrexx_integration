# Copyright (c) 2026, Goodvantage GmbH and contributors
# See license.txt

import base64
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import Barrier
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import frappe
from frappe.tests import IntegrationTestCase
from requests import HTTPError
from requests.models import Response

from payrexx_integration.api import (
	_get_payment_request_checkout_url,
	_sign,
	_verify,
	payment_success,
	payrexx_pay_url,
)
from payrexx_integration.gateway_selection import resolve_payrexx_settings
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
			"supported_currencies": "CHF,EUR,USD",
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _test_company() -> str:
	company = frappe.db.get_single_value("Global Defaults", "default_company")
	return company or frappe.db.get_value("Company", {}, "name")


def _ensure_test_customer() -> str:
	customer_name = "Payrexx Integration Test Customer"
	existing = frappe.db.get_value("Customer", {"customer_name": customer_name}, "name")
	if existing:
		return existing
	return (
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": customer_name,
				"customer_type": "Company",
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def _ensure_test_item() -> str:
	item_code = "PAYREXX-INTEGRATION-TEST"
	if frappe.db.exists("Item", item_code):
		return item_code
	item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
	return (
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_name": "Payrexx Integration Test",
				"item_group": item_group,
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"is_sales_item": 1,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def _create_submitted_test_sales_invoice():
	company = _test_company()
	company_currency = frappe.db.get_value("Company", company, "default_currency")
	sales_invoice = frappe.new_doc("Sales Invoice")
	sales_invoice.company = company
	sales_invoice.customer = _ensure_test_customer()
	sales_invoice.currency = company_currency
	sales_invoice.conversion_rate = 1
	sales_invoice.append(
		"items",
		{
			"item_code": _ensure_test_item(),
			"qty": 1,
			"rate": 100,
		},
	)
	sales_invoice.insert(ignore_permissions=True)
	sales_invoice.submit()
	return sales_invoice


def _ensure_test_payment_gateway_account(payment_gateway: str, sales_invoice) -> str:
	payment_account = frappe.db.get_value(
		"Account",
		{
			"company": sales_invoice.company,
			"account_type": ["in", ["Bank", "Cash"]],
			"is_group": 0,
			"disabled": 0,
		},
		"name",
	)
	if not payment_account:
		raise AssertionError(f"No payment account available for {sales_invoice.company}")

	gateway_account = frappe.db.get_value(
		"Payment Gateway Account",
		{
			"payment_gateway": payment_gateway,
			"currency": sales_invoice.currency,
			"company": sales_invoice.company,
		},
		"name",
	)
	if gateway_account:
		return gateway_account

	return (
		frappe.get_doc(
			{
				"doctype": "Payment Gateway Account",
				"is_default": 1,
				"payment_gateway": payment_gateway,
				"payment_account": payment_account,
				"currency": sales_invoice.currency,
				"company": sales_invoice.company,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def _create_submitted_test_payment_request():
	from erpnext.accounts.doctype.payment_request.payment_request import (
		PaymentRequest,
		make_payment_request,
	)

	payment_gateway = "_Test Gateway"
	if not frappe.db.exists("Payment Gateway", payment_gateway):
		frappe.get_doc({"doctype": "Payment Gateway", "gateway": payment_gateway}).insert(
			ignore_permissions=True
		)
	sales_invoice = _create_submitted_test_sales_invoice()
	gateway_account = _ensure_test_payment_gateway_account(payment_gateway, sales_invoice)
	with patch.object(PaymentRequest, "get_payment_url", return_value="https://pay.example/checkout"):
		payment_request = make_payment_request(
			dt="Sales Invoice",
			dn=sales_invoice.name,
			recipient_id="payrexx-test@example.com",
			mute_email=1,
			payment_gateway_account=gateway_account,
			submit_doc=1,
			return_doc=1,
		)
	return sales_invoice, payment_request


def _run_concurrent_manual_payrexx_checkout(
	site: str,
	sites_path: str,
	settings_name: str,
	payment_request_name: str,
	barrier: Barrier,
) -> str:
	frappe.init(site=site, sites_path=sites_path)
	frappe.connect()
	frappe.set_user("Administrator")
	frappe.flags.in_test = True
	try:
		# Document.submit() already owns its draft row before before_submit calls
		# the gateway controller. Reproduce that boundary without invoking
		# unrelated ERPNext submission behavior in this focused race test.
		payment_request = frappe.get_doc("Payment Request", payment_request_name, for_update=True)
		payment_request.docstatus = 1
		payment_request.status = "Requested"
		payment_request.outstanding_amount = payment_request.grand_total
		barrier.wait(timeout=30)

		settings = frappe.get_doc("Payrexx Settings", settings_name)
		checkout_url = settings.get_payment_url(
			amount=payment_request.grand_total,
			currency=payment_request.currency,
			payment_gateway=payment_request.payment_gateway,
			reference_doctype="Payment Request",
			reference_docname=payment_request.name,
		)
		frappe.db.set_value(
			"Payment Request",
			payment_request.name,
			{
				"docstatus": 1,
				"status": "Requested",
				"outstanding_amount": payment_request.grand_total,
				"payment_url": checkout_url,
			},
			update_modified=False,
		)
		frappe.db.commit()
		return "created"
	except frappe.QueryDeadlockError:
		frappe.db.rollback()
		return "rejected"
	except frappe.ValidationError:
		frappe.db.rollback()
		return "rejected"
	except Exception:
		frappe.db.rollback()
		raise
	finally:
		frappe.destroy()


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
		with (
			patch("payrexx_integration.api.frappe.db.exists", return_value=True),
			patch("payrexx_integration.api.frappe.db.get_value", return_value=1),
		):
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
		with patch(
			"payrexx_integration.payrexx_integration.payrexx.payrexx_client.frappe.conf",
			{"payrexx_allowed_api_hosts": ["api.pay.goodvantage.ch"]},
		):
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
		with patch(
			"payrexx_integration.payrexx_integration.payrexx.payrexx_client.frappe.conf",
			{"payrexx_allowed_api_hosts": ["api.pay.goodvantage.ch"]},
		):
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
		called_urls = []

		def fake_execute_request(method, url, **kwargs):
			called_urls.append(url)
			if "api.pay.goodvantage.ch" in url:
				response = Response()
				response.status_code = 403
				response.url = url
				raise HTTPError(response=response)
			return {"status": "success", "data": [{"id": 123, "link": "https://pay.example"}]}

		with (
			patch(
				"payrexx_integration.payrexx_integration.payrexx.payrexx_client.frappe.conf",
				{"payrexx_allowed_api_hosts": ["api.pay.goodvantage.ch"]},
			),
			patch(
				"payrexx_integration.payrexx_integration.payrexx.payrexx_client._execute_request",
				side_effect=fake_execute_request,
			),
		):
			client = PayrexxClient(
				instance="customer",
				api_secret="sk_test_dummy",
				api_version="v1.14",
				api_base_domain="pay.goodvantage.ch",
			)
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

	def test_gateway_payload_rejects_sub_cent_and_non_two_decimal_amounts(self):
		doc = frappe.get_doc("Payrexx Settings", self.settings_name)
		self.assertEqual(
			doc._build_create_gateway_payload(
				{"amount": "10.10", "currency": "CHF"},
				"PAYREXX-IR-TEST",
			)["amount"],
			1010,
		)
		with self.assertRaisesRegex(ValueError, "precision smaller"):
			doc._build_create_gateway_payload(
				{"amount": "10.001", "currency": "CHF"},
				"PAYREXX-IR-TEST",
			)

		currency = frappe.get_doc(
			{
				"doctype": "Currency",
				"currency_name": "TST3",
				"enabled": 1,
				"fraction_units": 1000,
			}
		).insert(ignore_if_duplicate=True)
		self.assertEqual(currency.fraction_units, 1000)
		with self.assertRaisesRegex(ValueError, "two-decimal"):
			doc._build_create_gateway_payload(
				{"amount": "10.100", "currency": "TST3"},
				"PAYREXX-IR-TEST",
			)

	def test_pay_url_explicit_gateway_name(self):
		other_settings = _ensure_settings("OtherGateway")
		with (
			patch("payrexx_integration.api.frappe.db.exists", return_value=True),
			patch("payrexx_integration.api.frappe.db.get_value", return_value=1),
		):
			url = payrexx_pay_url("ACC-SINV-2026-00001", gateway_name=other_settings)
		params = parse_qs(urlparse(url).query)
		self.assertEqual(params.get("gateway_name"), [other_settings])
		self.assertEqual(params.get("token"), [_sign("ACC-SINV-2026-00001", other_settings)])

	def test_gateway_resolver_explicit_choice_precedes_caller_site_config(self):
		settings = frappe._dict(name="Explicit")
		with (
			patch(
				"payrexx_integration.gateway_selection.frappe.conf",
				{"my_app_payrexx_gateway": "Configured"},
			),
			patch("payrexx_integration.gateway_selection.frappe.db.exists", return_value=True) as exists,
			patch("payrexx_integration.gateway_selection.frappe.get_cached_doc", return_value=settings),
			patch("payrexx_integration.gateway_selection.frappe.get_all") as get_all,
		):
			resolved = resolve_payrexx_settings(" Explicit ", site_config_key="my_app_payrexx_gateway")

		self.assertIs(resolved, settings)
		exists.assert_called_once_with("Payrexx Settings", "Explicit")
		get_all.assert_not_called()

	def test_gateway_resolver_uses_caller_site_config(self):
		settings = frappe._dict(name="Configured")
		with (
			patch(
				"payrexx_integration.gateway_selection.frappe.conf",
				{"my_app_payrexx_gateway": "Configured"},
			),
			patch("payrexx_integration.gateway_selection.frappe.db.exists", return_value=True) as exists,
			patch("payrexx_integration.gateway_selection.frappe.get_cached_doc", return_value=settings),
			patch("payrexx_integration.gateway_selection.frappe.get_all") as get_all,
		):
			resolved = resolve_payrexx_settings(site_config_key="my_app_payrexx_gateway")

		self.assertIs(resolved, settings)
		exists.assert_called_once_with("Payrexx Settings", "Configured")
		get_all.assert_not_called()

	def test_gateway_resolver_rejects_missing_explicit_choice_without_using_config(self):
		with (
			patch(
				"payrexx_integration.gateway_selection.frappe.conf",
				{"my_app_payrexx_gateway": "Configured"},
			),
			patch("payrexx_integration.gateway_selection.frappe.db.exists", return_value=False),
			patch("payrexx_integration.gateway_selection.frappe.get_all") as get_all,
			self.assertRaises(frappe.ValidationError) as exc,
		):
			resolve_payrexx_settings("Missing", site_config_key="my_app_payrexx_gateway")

		self.assertNotIn("site config", str(exc.exception))
		get_all.assert_not_called()

	def test_gateway_resolver_rejects_missing_configured_gateway_without_fallback(self):
		with (
			patch(
				"payrexx_integration.gateway_selection.frappe.conf",
				{"my_app_payrexx_gateway": "Missing"},
			),
			patch("payrexx_integration.gateway_selection.frappe.db.exists", return_value=False),
			patch("payrexx_integration.gateway_selection.frappe.get_all") as get_all,
			self.assertRaises(frappe.ValidationError) as exc,
		):
			resolve_payrexx_settings(site_config_key="my_app_payrexx_gateway")

		self.assertIn("my_app_payrexx_gateway", str(exc.exception))
		get_all.assert_not_called()

	def test_gateway_resolver_rejects_missing_settings(self):
		with (
			patch("payrexx_integration.gateway_selection.frappe.get_all", return_value=[]),
			self.assertRaises(frappe.ValidationError) as exc,
		):
			resolve_payrexx_settings()

		self.assertIn("No Payrexx Settings", str(exc.exception))

	def test_gateway_resolver_rejects_ambiguous_fallback(self):
		with (
			patch(
				"payrexx_integration.gateway_selection.frappe.get_all",
				return_value=["Live", "Sandbox"],
			),
			self.assertRaises(frappe.ValidationError) as exc,
		):
			resolve_payrexx_settings()

		self.assertIn("Multiple Payrexx gateways", str(exc.exception))

	def test_legacy_gateway_unbound_link_is_valid_but_requires_unambiguous_resolution(self):
		invoice_name = "ACC-SINV-LEGACY-00001"
		legacy_token = _sign(invoice_name)
		self.assertTrue(_verify(invoice_name, legacy_token))
		self.assertFalse(_verify(invoice_name, legacy_token, self.settings_name))

		settings = frappe._dict(name="Live")
		with (
			patch("payrexx_integration.gateway_selection.frappe.get_all", return_value=["Live"]),
			patch("payrexx_integration.gateway_selection.frappe.get_cached_doc", return_value=settings),
		):
			self.assertIs(resolve_payrexx_settings(), settings)

		with (
			patch(
				"payrexx_integration.gateway_selection.frappe.get_all",
				return_value=["Live", "Sandbox"],
			),
			self.assertRaises(frappe.ValidationError),
		):
			resolve_payrexx_settings()

	def test_legacy_gateway_unbound_link_rejects_ambiguity_before_paid_redirect(self):
		from payrexx_integration.api import pay_invoice

		invoice_name = "ACC-SINV-LEGACY-PAID-00001"
		invoice = frappe._dict(name=invoice_name, docstatus=1, outstanding_amount=0)
		with (
			patch("payrexx_integration.api.frappe.db.exists", return_value=True),
			patch("payrexx_integration.api.frappe.get_doc", return_value=invoice),
			patch(
				"payrexx_integration.api.resolve_payrexx_settings",
				side_effect=frappe.ValidationError("Multiple Payrexx gateways are configured."),
			) as resolve_settings,
			self.assertRaises(frappe.ValidationError),
		):
			pay_invoice(si=invoice_name, token=_sign(invoice_name))

		resolve_settings.assert_called_once_with(None)

	def test_legacy_gateway_unbound_link_accepts_single_gateway(self):
		from payrexx_integration.api import pay_invoice

		invoice_name = "ACC-SINV-LEGACY-PAID-00002"
		invoice = frappe._dict(name=invoice_name, docstatus=1, outstanding_amount=0)
		settings = frappe._dict(name="OnlyGateway")
		original_response = getattr(frappe.local, "response", None)
		try:
			frappe.local.response = {}
			with (
				patch("payrexx_integration.api.frappe.db.exists", return_value=True),
				patch("payrexx_integration.api.frappe.get_doc", return_value=invoice),
				patch("payrexx_integration.gateway_selection.frappe.get_all", return_value=[settings.name]),
				patch(
					"payrexx_integration.gateway_selection.frappe.get_cached_doc",
					return_value=settings,
				),
				patch(
					"payrexx_integration.api.get_public_url",
					return_value="https://example.test/payment-success",
				),
			):
				pay_invoice(si=invoice_name, token=_sign(invoice_name))
			response = dict(frappe.local.response)
		finally:
			frappe.local.response = original_response or {}

		self.assertEqual(response["type"], "redirect")
		self.assertEqual(response["location"], "https://example.test/payment-success")

	def test_pay_url_blank_invoice_returns_blank(self):
		self.assertEqual(payrexx_pay_url(None), "")
		self.assertEqual(payrexx_pay_url(""), "")

	def test_pay_url_missing_invoice_returns_blank_without_resolving_gateway(self):
		with patch("payrexx_integration.api.resolve_payrexx_settings") as resolve_settings:
			self.assertEqual(payrexx_pay_url("ACC-SINV-DOES-NOT-EXIST"), "")

		resolve_settings.assert_not_called()

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

	def test_gateway_account_filter_is_company_and_currency_specific(self):
		from payrexx_integration.api import _gateway_account_filter

		sales_invoice = frappe._dict(company="Test Company", currency="EUR")
		expected = {
			"payment_gateway": "Payrexx-Live",
			"company": "Test Company",
			"currency": "EUR",
		}
		with patch("payrexx_integration.api.frappe.db.exists", return_value=True) as exists:
			self.assertEqual(_gateway_account_filter(sales_invoice, "Payrexx-Live"), expected)

		exists.assert_called_once_with("Payment Gateway Account", expected)

	def test_pay_link_flow_preserves_conflicting_staff_draft_payment_request(self):
		from payrexx_integration.api import _get_or_create_payment_request

		sales_invoice = _create_submitted_test_sales_invoice()
		staff_gateway = "Payrexx-Staff-Draft"
		draft = frappe.get_doc(
			{
				"doctype": "Payment Request",
				"payment_request_type": "Inward",
				"reference_doctype": "Sales Invoice",
				"reference_name": sales_invoice.name,
				"payment_gateway": staff_gateway,
				"currency": sales_invoice.currency,
				"company": sales_invoice.company,
				"grand_total": sales_invoice.outstanding_amount,
				"party_type": "Customer",
				"party": sales_invoice.customer,
				"party_name": sales_invoice.customer_name,
				"email_to": "staff@example.com",
				"subject": "Staff-created payment request",
				"message": "Preserve this draft",
				"mute_email": 1,
			}
		).insert(ignore_permissions=True)

		with (
			patch(
				"erpnext.accounts.doctype.payment_request.payment_request.make_payment_request"
			) as make_payment_request,
			self.assertRaises(frappe.ValidationError),
		):
			_get_or_create_payment_request(sales_invoice, self.settings_name)

		make_payment_request.assert_not_called()
		self.assertTrue(frappe.db.exists("Payment Request", draft.name))
		draft.reload()
		self.assertEqual(draft.docstatus, 0)
		self.assertEqual(draft.payment_gateway, staff_gateway)
		self.assertEqual(draft.message, "Preserve this draft")

	def test_first_pay_invoice_click_creates_exactly_one_provider_checkout_and_request(self):
		from payrexx_integration.api import pay_invoice
		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		sales_invoice = _create_submitted_test_sales_invoice()
		payment_gateway = "Payrexx-" + self.settings_name
		_ensure_test_payment_gateway_account(payment_gateway, sales_invoice)
		payment_request_filters = {
			"reference_doctype": "Sales Invoice",
			"reference_name": sales_invoice.name,
			"payment_gateway": payment_gateway,
		}
		self.assertEqual(frappe.get_all("Payment Request", filters=payment_request_filters), [])

		checkout_url = "https://pay.example/first-click-checkout"
		client = Mock()
		client.create_gateway.return_value = {
			"id": 424242,
			"hash": "first-click-hash",
			"link": checkout_url,
		}
		original_response = getattr(frappe.local, "response", None)
		original_commit = getattr(frappe.local.flags, "commit", False)
		try:
			frappe.local.response = {}
			frappe.local.flags.commit = False
			with patch.object(ps_module.PayrexxSettings, "_client", return_value=client):
				pay_invoice(
					si=sales_invoice.name,
					token=_sign(sales_invoice.name, self.settings_name),
					gateway_name=self.settings_name,
				)
			response = dict(frappe.local.response)
			commit_requested = frappe.local.flags.commit
		finally:
			frappe.local.response = original_response or {}
			frappe.local.flags.commit = original_commit

		client.create_gateway.assert_called_once()
		payment_request_names = frappe.get_all(
			"Payment Request",
			filters=payment_request_filters,
			pluck="name",
		)
		self.assertEqual(len(payment_request_names), 1)
		payment_request = frappe.get_doc("Payment Request", payment_request_names[0])
		self.assertEqual(payment_request.docstatus, 1)
		self.assertEqual(payment_request.payment_url, checkout_url)

		integration_requests = frappe.get_all(
			"Integration Request",
			filters={
				"reference_doctype": "Payment Request",
				"reference_docname": payment_request.name,
				"integration_request_service": "Payrexx",
			},
			fields=["name", "data"],
		)
		self.assertEqual(len(integration_requests), 1)
		request_data = frappe.parse_json(integration_requests[0].data) or {}
		self.assertEqual(request_data["payrexx_gateway_id"], 424242)
		self.assertEqual(request_data["payrexx_checkout_url"], checkout_url)
		self.assertEqual(request_data["payrexx_gateway_amount"], 10000)
		self.assertEqual(request_data["payrexx_gateway_currency"], sales_invoice.currency)
		self.assertEqual(response["type"], "redirect")
		self.assertEqual(response["location"], checkout_url)
		self.assertTrue(commit_requested)

	def test_payment_request_checkout_reuses_url_created_on_submission(self):
		sales_invoice = Mock()
		sales_invoice.name = "SINV-TEST"
		payment_request = Mock()
		payment_request.name = "PAY-REQ-TEST"
		payment_request.docstatus = 1
		payment_request.payment_url = "https://pay.example/only-checkout"
		active_request = frappe._dict(
			name="PAYREXX-IR-TEST",
			status="Queued",
			reference_doctype="Payment Request",
			reference_docname=payment_request.name,
			data=frappe.as_json(
				{
					"amount": 100,
					"currency": "CHF",
					"payment_gateway": "Payrexx-TestGW",
					"reference_doctype": "Payment Request",
					"reference_docname": payment_request.name,
					"payrexx_settings": "TestGW",
					"payrexx_gateway_id": 123,
					"payrexx_gateway_hash": "hash",
					"payrexx_checkout_url": payment_request.payment_url,
					"payrexx_gateway_amount": 10000,
					"payrexx_gateway_currency": "CHF",
				}
			),
		)

		with (
			patch(
				"payrexx_integration.api.frappe.get_doc",
				side_effect=(payment_request, sales_invoice),
			),
			patch(
				"payrexx_integration.api._validate_payment_request_checkout_state",
				return_value=(10000, "CHF"),
			),
			patch(
				"payrexx_integration.api._get_active_checkout_requests",
				return_value=[active_request],
			),
			patch(
				"payrexx_integration.api._get_active_payrexx_payment_requests",
				return_value=[frappe._dict(name=payment_request.name)],
			),
		):
			checkout_url = _get_payment_request_checkout_url(payment_request, sales_invoice, "TestGW")

		self.assertEqual(checkout_url, payment_request.payment_url)
		payment_request.get_payment_url.assert_not_called()
		payment_request.db_set.assert_not_called()

	def test_payment_request_without_url_recovers_stored_checkout(self):
		sales_invoice = Mock()
		sales_invoice.name = "SINV-TEST"
		payment_request = Mock()
		payment_request.name = "PAY-REQ-TEST"
		payment_request.docstatus = 1
		payment_request.payment_url = ""
		active_request = frappe._dict(
			name="PAYREXX-IR-TEST",
			status="Queued",
			reference_doctype="Payment Request",
			reference_docname=payment_request.name,
			data=frappe.as_json(
				{
					"amount": 100,
					"currency": "CHF",
					"payment_gateway": "Payrexx-TestGW",
					"reference_doctype": "Payment Request",
					"reference_docname": payment_request.name,
					"payrexx_settings": "TestGW",
					"payrexx_gateway_id": 123,
					"payrexx_gateway_hash": "hash",
					"payrexx_checkout_url": "https://pay.example/recovered",
					"payrexx_gateway_amount": 10000,
					"payrexx_gateway_currency": "CHF",
				}
			),
		)

		with (
			patch(
				"payrexx_integration.api.frappe.get_doc",
				side_effect=(payment_request, sales_invoice),
			),
			patch(
				"payrexx_integration.api._validate_payment_request_checkout_state",
				return_value=(10000, "CHF"),
			),
			patch(
				"payrexx_integration.api._get_active_checkout_requests",
				return_value=[active_request],
			),
			patch(
				"payrexx_integration.api._get_active_payrexx_payment_requests",
				return_value=[frappe._dict(name=payment_request.name)],
			),
		):
			checkout_url = _get_payment_request_checkout_url(payment_request, sales_invoice, "TestGW")

		self.assertEqual(checkout_url, "https://pay.example/recovered")
		payment_request.get_payment_url.assert_not_called()
		payment_request.db_set.assert_called_once_with(
			"payment_url", "https://pay.example/recovered", update_modified=False
		)

	def test_payment_request_without_url_does_not_duplicate_unknown_active_checkout(self):
		sales_invoice = Mock()
		sales_invoice.name = "SINV-TEST"
		payment_request = Mock()
		payment_request.name = "PAY-REQ-TEST"
		payment_request.docstatus = 1
		payment_request.payment_url = ""
		active_request = frappe._dict(name="PAYREXX-IR-LEGACY", data="{}")

		with (
			patch(
				"payrexx_integration.api.frappe.get_doc",
				side_effect=(payment_request, sales_invoice),
			),
			patch(
				"payrexx_integration.api._validate_payment_request_checkout_state",
				return_value=(10000, "CHF"),
			),
			patch(
				"payrexx_integration.api._get_active_checkout_requests",
				return_value=[active_request],
			),
			patch(
				"payrexx_integration.api._get_active_payrexx_payment_requests",
				return_value=[frappe._dict(name=payment_request.name)],
			),
			self.assertRaises(frappe.ValidationError),
		):
			_get_payment_request_checkout_url(payment_request, sales_invoice, "TestGW")

		payment_request.get_payment_url.assert_not_called()

	# ---------------------------------------------------- callback (full path)

	def test_callback_marks_integration_request_completed(self):
		_sales_invoice, payment_request = _create_submitted_test_payment_request()
		# Set up an Integration Request the callback should resolve to
		ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Queued",
				"reference_doctype": "Payment Request",
				"reference_docname": payment_request.name,
				"data": json.dumps(
					{
						"payrexx_gateway_id": 999,
						"payrexx_gateway_amount": int(payment_request.grand_total * 100),
						"payrexx_gateway_currency": payment_request.currency,
					}
				),
			}
		).insert(ignore_permissions=True)

		body = json.dumps(
			{
				"transaction": {
					"id": 12345,
					"status": "confirmed",
					"amount": int(payment_request.grand_total * 100),
					"currency": payment_request.currency,
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

	def test_completed_and_chargeback_request_ignore_non_chargeback_webhook_replays(self):
		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		confirmed = {"id": 12345, "status": "confirmed", "amount": 10000, "currency": "CHF"}
		integration_request = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Completed",
				"data": frappe.as_json({"payrexx_transaction": confirmed}),
			}
		).insert(ignore_permissions=True)

		def send_webhook(status):
			body = frappe.as_json(
				{
					"transaction": {
						"id": 12345,
						"status": status,
						"referenceId": integration_request.name,
						"invoice": {"referenceId": integration_request.name},
					}
				}
			).encode()
			signature = base64.b64encode(hmac.new(b"whk_test_dummy", body, hashlib.sha256).digest()).decode(
				"ascii"
			)

			class _FakeRequest:
				def __init__(self):
					self.args = {}
					self.form = {}

				def get_data(self):
					return body

			original_request = getattr(frappe.local, "request", None)
			frappe.local.request = _FakeRequest()
			try:
				with patch.object(
					frappe,
					"get_request_header",
					return_value=signature,
				):
					self.assertEqual(ps_module.callback(gateway_name=GATEWAY_NAME), {"ok": True})
			finally:
				if original_request is None:
					delattr(frappe.local, "request")
				else:
					frappe.local.request = original_request

		for status in (
			"authorized",
			"reserved",
			"waiting",
			"cancelled",
			"declined",
			"error",
			"expired",
			"refunded",
		):
			with self.subTest(status=status):
				send_webhook(status)
				integration_request.reload()
				self.assertEqual(integration_request.status, "Completed")
				self.assertEqual(
					(frappe.parse_json(integration_request.data) or {})["payrexx_transaction"],
					confirmed,
				)

		send_webhook("chargeback")
		integration_request.reload()
		self.assertEqual(integration_request.status, "Failed")
		chargeback_data = frappe.parse_json(integration_request.data) or {}
		chargeback_transaction = chargeback_data["payrexx_transaction"]
		self.assertEqual(chargeback_transaction["status"], "chargeback")
		self.assertEqual(integration_request.error, ps_module.CHARGEBACK_ERROR)

		for status in (
			"confirmed",
			"authorized",
			"reserved",
			"waiting",
			"cancelled",
			"declined",
			"error",
			"expired",
			"refunded",
		):
			with self.subTest(replay_after_chargeback=status):
				send_webhook(status)
				integration_request.reload()
				self.assertEqual(integration_request.status, "Failed")
				self.assertEqual(integration_request.error, ps_module.CHARGEBACK_ERROR)
				self.assertEqual(
					(frappe.parse_json(integration_request.data) or {})["payrexx_transaction"],
					chargeback_transaction,
				)

		# A duplicate chargeback is idempotent and keeps the first evidence.
		send_webhook("chargeback")
		integration_request.reload()
		self.assertEqual(
			(frappe.parse_json(integration_request.data) or {})["payrexx_transaction"],
			chargeback_transaction,
		)
		with patch.object(ps_module, "_resolve_settings") as resolve_settings:
			self.assertFalse(ps_module.reconcile_integration_request(integration_request.name))
		resolve_settings.assert_not_called()

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
		_sales_invoice, payment_request = _create_submitted_test_payment_request()
		payment_request.db_set("payment_gateway", "Payrexx-" + self.settings_name)
		payment_request.reload()

		class _FakeClient:
			def create_gateway(self, payload):
				return {"id": 4242, "hash": "h", "link": "https://pay.example/checkout"}

		with patch.object(type(settings), "_client", return_value=_FakeClient()):
			link = settings.get_payment_url(
				amount=payment_request.grand_total,
				currency=payment_request.currency,
				payment_gateway="Payrexx-" + self.settings_name,
				reference_doctype="Payment Request",
				reference_docname=payment_request.name,
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
		self.assertEqual(data.get("payrexx_checkout_url"), "https://pay.example/checkout")
		self.assertEqual(data.get("payrexx_gateway_amount"), int(payment_request.grand_total * 100))
		self.assertEqual(data.get("payrexx_gateway_currency"), payment_request.currency)

	def test_get_payment_url_rejects_sales_order_payment_request_before_gateway_creation(self):
		settings = frappe.get_doc("Payrexx Settings", self.settings_name)
		client = Mock()
		with (
			patch.object(
				frappe.db,
				"get_value",
				return_value=frappe._dict(
					reference_doctype="Sales Order",
					reference_name="SO-TEST",
				),
			),
			patch.object(type(settings), "_client", return_value=client),
			self.assertRaisesRegex(frappe.ValidationError, "Sales Invoices"),
		):
			settings.get_payment_url(
				amount=10,
				currency="CHF",
				reference_doctype="Payment Request",
				reference_docname="PR-SALES-ORDER",
			)

		client.create_gateway.assert_not_called()

	def test_get_payment_url_rejects_direct_non_payment_request_reference(self):
		settings = frappe.get_doc("Payrexx Settings", self.settings_name)
		client = Mock()
		with (
			patch.object(type(settings), "_client", return_value=client),
			self.assertRaisesRegex(frappe.ValidationError, "Sales Invoices"),
		):
			settings.get_payment_url(
				amount=10,
				currency="CHF",
				reference_doctype="Customer",
				reference_docname="CUSTOMER-TEST",
			)

		client.create_gateway.assert_not_called()

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
		_sales_invoice, payment_request = _create_submitted_test_payment_request()
		ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Queued",
				"reference_doctype": "Payment Request",
				"reference_docname": payment_request.name,
				"data": json.dumps(
					{
						"payrexx_gateway_id": 999,
						"payrexx_gateway_amount": int(payment_request.grand_total * 100),
						"payrexx_gateway_currency": payment_request.currency,
					}
				),
			}
		).insert(ignore_permissions=True)

		body = json.dumps(
			{
				"transaction": {
					"id": 12345,
					"status": "confirmed",
					"amount": int(payment_request.grand_total * 100),
					"currency": payment_request.currency,
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

	def test_confirmation_retries_whole_locked_unit_after_deadlock(self):
		ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Queued",
				"data": json.dumps({"amount": 100, "currency": "CHF"}),
			}
		).insert(ignore_permissions=True)

		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		transaction = {"id": 12345, "status": "confirmed", "amount": 10000, "currency": "CHF"}
		calls = []
		complete_locked = ps_module._complete_locked_integration_request

		def deadlock_then_complete(integration_request_name, callback_transaction):
			calls.append((integration_request_name, callback_transaction))
			if len(calls) == 1:
				raise frappe.QueryDeadlockError((1020, "Record has changed since last read"))
			return complete_locked(integration_request_name, callback_transaction)

		with (
			patch.object(
				ps_module,
				"_complete_locked_integration_request",
				side_effect=deadlock_then_complete,
			),
			patch.object(ps_module.frappe.db, "rollback") as rollback,
			patch.object(ps_module.time, "sleep") as sleep,
		):
			ps_module._complete_integration_request(ir.name, transaction)

		self.assertEqual(calls, [(ir.name, transaction), (ir.name, transaction)])
		rollback.assert_called_once()
		sleep.assert_called_once_with(0.25)
		ir.reload()
		self.assertEqual(ir.status, "Failed")
		self.assertEqual((frappe.parse_json(ir.data) or {})["payrexx_transaction"], transaction)
		self.assertEqual(
			(frappe.parse_json(ir.data) or {})["payrexx_settlement_conflict"]["code"],
			"payment_request_reference_required",
		)

	def test_deadlock_retry_is_bounded_and_rolls_back_every_failed_attempt(self):
		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		operation = Mock(side_effect=frappe.QueryDeadlockError((1020, "Record has changed since last read")))
		with (
			patch.object(ps_module.frappe.db, "rollback") as rollback,
			patch.object(ps_module.time, "sleep") as sleep,
			self.assertRaises(frappe.QueryDeadlockError),
		):
			ps_module._run_with_deadlock_retry(operation)

		self.assertEqual(operation.call_count, ps_module.DEADLOCK_MAX_ATTEMPTS)
		self.assertEqual(rollback.call_count, ps_module.DEADLOCK_MAX_ATTEMPTS)
		self.assertEqual([item.args[0] for item in sleep.call_args_list], [0.25, 0.5])

	def test_deadlock_retry_completes_request_and_creates_exactly_one_payment_entry(self):
		from erpnext.accounts.doctype.payment_request.payment_request import (
			PaymentRequest,
			make_payment_request,
		)

		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		payment_gateway = "_Test Gateway"
		if not frappe.db.exists("Payment Gateway", payment_gateway):
			frappe.get_doc({"doctype": "Payment Gateway", "gateway": payment_gateway}).insert(
				ignore_permissions=True
			)
		sales_invoice = _create_submitted_test_sales_invoice()
		gateway_account = _ensure_test_payment_gateway_account(payment_gateway, sales_invoice)
		with patch.object(PaymentRequest, "get_payment_url", return_value="https://pay.example/checkout"):
			payment_request = make_payment_request(
				dt="Sales Invoice",
				dn=sales_invoice.name,
				recipient_id="payrexx-test@example.com",
				mute_email=1,
				payment_gateway_account=gateway_account,
				submit_doc=1,
				return_doc=1,
			)

		integration_request = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Queued",
				"reference_doctype": "Payment Request",
				"reference_docname": payment_request.name,
				"data": json.dumps(
					{"amount": payment_request.grand_total, "currency": payment_request.currency}
				),
			}
		).insert(ignore_permissions=True)
		confirmed = {
			"id": 54321,
			"status": "confirmed",
			"amount": round(payment_request.grand_total * 100),
			"currency": payment_request.currency,
		}
		savepoint = "payrexx_deadlock_retry"
		frappe.db.savepoint(savepoint)
		real_rollback = frappe.db.rollback
		real_set_as_paid = PaymentRequest.set_as_paid
		settlement_attempts = []
		rollback_statuses = []

		def settle_then_deadlock(request):
			payment_entry = real_set_as_paid(request)
			settlement_attempts.append(payment_entry.name)
			if len(settlement_attempts) == 1:
				raise frappe.QueryDeadlockError((1213, "Deadlock found when trying to get lock"))
			return payment_entry

		def rollback_to_savepoint():
			rollback_statuses.append(
				frappe.db.get_value("Integration Request", integration_request.name, "status")
			)
			real_rollback(save_point=savepoint)
			rollback_statuses.append(
				frappe.db.get_value("Integration Request", integration_request.name, "status")
			)

		with (
			patch.object(PaymentRequest, "set_as_paid", autospec=True, side_effect=settle_then_deadlock),
			patch.object(ps_module.frappe.db, "rollback", side_effect=rollback_to_savepoint) as rollback,
			patch.object(ps_module.time, "sleep") as sleep,
		):
			ps_module._complete_integration_request(integration_request.name, confirmed)

		self.assertEqual(len(settlement_attempts), 2)
		self.assertEqual(rollback_statuses, ["Completed", "Queued"])
		rollback.assert_called_once_with()
		sleep.assert_called_once_with(0.25)
		integration_request.reload()
		payment_request.reload()
		sales_invoice.reload()
		payment_entries = set(
			frappe.get_all(
				"Payment Entry Reference",
				filters={"payment_request": payment_request.name, "docstatus": 1},
				pluck="parent",
			)
		)
		self.assertEqual(integration_request.status, "Completed")
		self.assertEqual(
			(frappe.parse_json(integration_request.data) or {})["payrexx_transaction"], confirmed
		)
		self.assertEqual(payment_request.status, "Paid")
		self.assertEqual(payment_request.outstanding_amount, 0)
		self.assertEqual(sales_invoice.outstanding_amount, 0)
		self.assertEqual(len(payment_entries), 1)
		payment_entry_name = payment_entries.pop()
		self.assertEqual(frappe.db.get_value("Payment Entry", payment_entry_name, "docstatus"), 1)
		self.assertEqual(
			(frappe.parse_json(integration_request.data) or {})["payrexx_payment_entry"],
			payment_entry_name,
		)

	def test_non_payment_request_keeps_authorization_hook(self):
		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		integration_request = frappe._dict(
			reference_doctype="Donation",
			reference_docname="NPO-DTN-TEST",
		)
		reference = Mock()
		with (
			patch.object(ps_module, "_payment_authorization_user", return_value=nullcontext()),
			patch.object(ps_module.frappe, "get_doc", return_value=reference),
		):
			ps_module._on_payment_authorized(integration_request, "Completed")

		reference.run_method.assert_called_once_with("on_payment_authorized", "Completed")

	def test_payment_request_confirmation_and_chargeback_are_idempotent(self):
		from erpnext.accounts.doctype.payment_request.payment_request import (
			PaymentRequest,
			make_payment_request,
		)

		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		if not frappe.db.exists("Payment Gateway", "_Test Gateway"):
			frappe.get_doc({"doctype": "Payment Gateway", "gateway": "_Test Gateway"}).insert(
				ignore_permissions=True
			)
		sales_invoice = _create_submitted_test_sales_invoice()
		payment_account = frappe.db.get_value(
			"Account",
			{
				"company": sales_invoice.company,
				"account_type": ["in", ["Bank", "Cash"]],
				"is_group": 0,
				"disabled": 0,
			},
			"name",
		)
		self.assertTrue(payment_account)
		gateway_account = frappe.db.get_value(
			"Payment Gateway Account",
			{
				"payment_gateway": "_Test Gateway",
				"currency": sales_invoice.currency,
				"company": sales_invoice.company,
			},
			"name",
		)
		if not gateway_account:
			gateway_account = (
				frappe.get_doc(
					{
						"doctype": "Payment Gateway Account",
						"is_default": 1,
						"payment_gateway": "_Test Gateway",
						"payment_account": payment_account,
						"currency": sales_invoice.currency,
						"company": sales_invoice.company,
					}
				)
				.insert(ignore_permissions=True)
				.name
			)

		with patch.object(PaymentRequest, "get_payment_url", return_value="https://pay.example/checkout"):
			payment_request = make_payment_request(
				dt="Sales Invoice",
				dn=sales_invoice.name,
				recipient_id="payrexx-test@example.com",
				mute_email=1,
				payment_gateway_account=gateway_account,
				submit_doc=1,
				return_doc=1,
			)

		integration_request = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Queued",
				"reference_doctype": "Payment Request",
				"reference_docname": payment_request.name,
				"data": json.dumps(
					{"amount": payment_request.grand_total, "currency": payment_request.currency}
				),
			}
		).insert(ignore_permissions=True)
		confirmed = {
			"id": 12345,
			"status": "confirmed",
			"amount": round(payment_request.grand_total * 100),
			"currency": payment_request.currency,
		}

		ps_module._complete_integration_request(integration_request.name, confirmed)
		ps_module._complete_integration_request(integration_request.name, confirmed)

		payment_request.reload()
		sales_invoice.reload()
		payment_entries = frappe.get_all(
			"Payment Entry Reference",
			filters={"payment_request": payment_request.name, "docstatus": 1},
			pluck="parent",
		)
		self.assertEqual(payment_request.status, "Paid")
		self.assertEqual(payment_request.outstanding_amount, 0)
		self.assertEqual(sales_invoice.outstanding_amount, 0)
		self.assertEqual(len(set(payment_entries)), 1)
		self.assertEqual(frappe.db.get_value("Payment Entry", payment_entries[0], "docstatus"), 1)

		chargeback = {"id": 12345, "status": "chargeback"}
		ps_module._mark_chargeback(integration_request.name, chargeback)
		ps_module._mark_chargeback(integration_request.name, chargeback)
		ps_module._complete_integration_request(integration_request.name, confirmed)

		integration_request.reload()
		self.assertEqual(integration_request.status, "Failed")
		self.assertEqual(integration_request.error, ps_module.CHARGEBACK_ERROR)
		self.assertEqual(
			(frappe.parse_json(integration_request.data) or {})["payrexx_transaction"],
			chargeback,
		)
		self.assertEqual(frappe.db.get_value("Payment Entry", payment_entries[0], "docstatus"), 1)
		chargeback_todos = frappe.get_all(
			"ToDo",
			filters={
				"reference_type": "Integration Request",
				"reference_name": integration_request.name,
				"description": ["like", f"{ps_module.CHARGEBACK_TODO_MARKER}%"],
			},
			fields=["priority", "status"],
		)
		self.assertEqual(chargeback_todos, [{"priority": "High", "status": "Open"}])

	def test_settlement_conflict_is_structured_terminal_and_idempotent(self):
		_sales_invoice, payment_request = _create_submitted_test_payment_request()
		expected_amount = int(payment_request.grand_total * 100)
		integration_request = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Queued",
				"reference_doctype": "Payment Request",
				"reference_docname": payment_request.name,
				"data": json.dumps(
					{
						"payrexx_gateway_amount": expected_amount,
						"payrexx_gateway_currency": payment_request.currency,
					}
				),
			}
		).insert(ignore_permissions=True)

		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		conflicting_transaction = {
			"id": 80001,
			"status": "confirmed",
			"amount": expected_amount + 1,
			"currency": payment_request.currency,
		}
		ps_module._complete_integration_request(integration_request.name, conflicting_transaction)
		integration_request.reload()
		first_data = frappe.parse_json(integration_request.data) or {}
		marker = first_data[ps_module.SETTLEMENT_CONFLICT_DATA_KEY]
		self.assertEqual(integration_request.status, "Failed")
		self.assertTrue(marker["terminal"])
		self.assertEqual(marker["version"], 1)
		self.assertEqual(marker["code"], "amount_mismatch")
		self.assertEqual(marker["evidence"]["provider_amount"], expected_amount + 1)

		# Even matching evidence on a later authentic replay cannot reopen a
		# conflict after accounting review has become necessary.
		ps_module._complete_integration_request(
			integration_request.name,
			{
				"id": 80002,
				"status": "confirmed",
				"amount": expected_amount,
				"currency": payment_request.currency,
			},
		)
		integration_request.reload()
		second_data = frappe.parse_json(integration_request.data) or {}
		self.assertEqual(second_data[ps_module.SETTLEMENT_CONFLICT_DATA_KEY], marker)
		self.assertEqual(second_data["payrexx_transaction"], conflicting_transaction)

		# The public callback still authenticates a replay, then preserves the
		# terminal marker instead of moving the request back to Authorized.
		replay_body = json.dumps(
			{
				"transaction": {
					"id": 80003,
					"status": "authorized",
					"referenceId": integration_request.name,
				}
			}
		).encode("utf-8")
		replay_signature = base64.b64encode(
			hmac.new(b"whk_test_dummy", replay_body, hashlib.sha256).digest()
		).decode("ascii")

		class _ReplayRequest:
			def __init__(self):
				self.args = {}
				self.form = {}

			def get_data(self):
				return replay_body

		original_request = getattr(frappe.local, "request", None)
		original_header = frappe.get_request_header
		frappe.local.request = _ReplayRequest()
		frappe.get_request_header = lambda name, default="": (  # type: ignore[assignment]
			replay_signature if name == "X-Webhook-Signature" else default
		)
		try:
			self.assertEqual(ps_module.callback(gateway_name=GATEWAY_NAME), {"ok": True})
		finally:
			frappe.get_request_header = original_header  # type: ignore[assignment]
			if original_request is None:
				delattr(frappe.local, "request")
			else:
				frappe.local.request = original_request
		integration_request.reload()
		replayed_data = frappe.parse_json(integration_request.data) or {}
		self.assertEqual(integration_request.status, "Failed")
		self.assertEqual(replayed_data[ps_module.SETTLEMENT_CONFLICT_DATA_KEY], marker)
		self.assertEqual(replayed_data["payrexx_transaction"], conflicting_transaction)
		self.assertEqual(
			len(
				frappe.get_all(
					"ToDo",
					filters={
						"reference_type": "Integration Request",
						"reference_name": integration_request.name,
						"description": [
							"like",
							f"{ps_module.SETTLEMENT_CONFLICT_TODO_MARKER}%",
						],
					},
					pluck="name",
				)
			),
			1,
		)
		self.assertFalse(
			frappe.db.exists(
				"Payment Entry Reference",
				{"payment_request": payment_request.name, "docstatus": 1},
			)
		)

	def test_confirmation_requires_submitted_payment_request_and_source_document(self):
		sales_invoice = _create_submitted_test_sales_invoice()
		draft_request = frappe.get_doc(
			{
				"doctype": "Payment Request",
				"payment_request_type": "Inward",
				"reference_doctype": "Sales Invoice",
				"reference_name": sales_invoice.name,
				"payment_gateway": "_Test Gateway",
				"currency": sales_invoice.currency,
				"company": sales_invoice.company,
				"grand_total": sales_invoice.outstanding_amount,
				"party_type": "Customer",
				"party": sales_invoice.customer,
				"party_name": sales_invoice.customer_name,
				"email_to": "payrexx-test@example.com",
				"mute_email": 1,
			}
		).insert(ignore_permissions=True)
		integration_request = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Queued",
				"reference_doctype": "Payment Request",
				"reference_docname": draft_request.name,
				"data": json.dumps(
					{
						"payrexx_gateway_amount": int(draft_request.grand_total * 100),
						"payrexx_gateway_currency": draft_request.currency,
					}
				),
			}
		).insert(ignore_permissions=True)

		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		ps_module._complete_integration_request(
			integration_request.name,
			{
				"id": 81001,
				"status": "confirmed",
				"amount": int(draft_request.grand_total * 100),
				"currency": draft_request.currency,
			},
		)
		integration_request.reload()
		self.assertEqual(
			(frappe.parse_json(integration_request.data) or {})[ps_module.SETTLEMENT_CONFLICT_DATA_KEY][
				"code"
			],
			"payment_request_not_active",
		)

		_source_invoice, submitted_request = _create_submitted_test_payment_request()
		frappe.db.set_value("Sales Invoice", submitted_request.reference_name, "docstatus", 0)
		source_request = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Queued",
				"reference_doctype": "Payment Request",
				"reference_docname": submitted_request.name,
				"data": json.dumps(
					{
						"payrexx_gateway_amount": int(submitted_request.grand_total * 100),
						"payrexx_gateway_currency": submitted_request.currency,
					}
				),
			}
		).insert(ignore_permissions=True)
		ps_module._complete_integration_request(
			source_request.name,
			{
				"id": 81002,
				"status": "confirmed",
				"amount": int(submitted_request.grand_total * 100),
				"currency": submitted_request.currency,
			},
		)
		source_request.reload()
		self.assertEqual(
			(frappe.parse_json(source_request.data) or {})[ps_module.SETTLEMENT_CONFLICT_DATA_KEY]["code"],
			"source_document_not_submitted",
		)

	def test_confirmation_rejects_ambiguous_foreign_currency_accounting_path(self):
		_sales_invoice, payment_request = _create_submitted_test_payment_request()
		foreign_currency = "EUR" if payment_request.currency != "EUR" else "CHF"
		frappe.db.set_value(
			"Payment Request",
			payment_request.name,
			"party_account_currency",
			foreign_currency,
		)
		payment_request.reload()
		expected_amount = int(payment_request.grand_total * 100)
		integration_request = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Queued",
				"reference_doctype": "Payment Request",
				"reference_docname": payment_request.name,
				"data": json.dumps(
					{
						"payrexx_gateway_amount": expected_amount,
						"payrexx_gateway_currency": payment_request.currency,
					}
				),
			}
		).insert(ignore_permissions=True)

		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		ps_module._complete_integration_request(
			integration_request.name,
			{
				"id": 82001,
				"status": "confirmed",
				"amount": expected_amount,
				"currency": payment_request.currency,
			},
		)
		integration_request.reload()
		marker = (frappe.parse_json(integration_request.data) or {})[ps_module.SETTLEMENT_CONFLICT_DATA_KEY]
		self.assertEqual(marker["code"], "unsupported_currency_context")
		self.assertEqual(
			marker["evidence"]["payment_request"]["party_account_currency"],
			foreign_currency,
		)
		self.assertFalse(
			frappe.db.exists(
				"Payment Entry Reference",
				{"payment_request": payment_request.name, "docstatus": 1},
			)
		)

	def test_success_reconciliation_marks_integration_request_completed(self):
		_sales_invoice, payment_request = _create_submitted_test_payment_request()
		ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payrexx",
				"status": "Queued",
				"reference_doctype": "Payment Request",
				"reference_docname": payment_request.name,
				"data": json.dumps(
					{
						"payrexx_gateway_id": 999,
						"payment_gateway": "Payrexx-" + GATEWAY_NAME,
						"payrexx_gateway_amount": int(payment_request.grand_total * 100),
						"payrexx_gateway_currency": payment_request.currency,
					}
				),
			}
		).insert(ignore_permissions=True)

		class _FakeClient:
			def retrieve_gateway(self, gateway_id: int) -> dict:
				self.gateway_id = gateway_id
				return {
					"id": gateway_id,
					"status": "waiting",
					"invoices": [
						{
							"transactions": [
								{
									"amount": int(payment_request.grand_total * 100),
									"currency": payment_request.currency,
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
		original_commit = getattr(frappe.local.flags, "commit", False)
		try:
			frappe.conf.host_name = "https://demo.example.test"
			frappe.local.response = {}
			frappe.local.flags.commit = False
			with patch.object(ps_module, "reconcile_integration_request", return_value=True):
				payment_success(ir=ir.name, gateway_name=GATEWAY_NAME)
			response = dict(frappe.local.response)
			commit_requested = frappe.local.flags.commit
		finally:
			if original_host_name is None:
				frappe.conf.pop("host_name", None)
			else:
				frappe.conf.host_name = original_host_name
			if original_response is None:
				frappe.local.response = {}
			else:
				frappe.local.response = original_response
			frappe.local.flags.commit = original_commit

		self.assertEqual(response["type"], "redirect")
		self.assertEqual(response["location"], return_url)
		self.assertTrue(commit_requested)

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
		original_commit = getattr(frappe.local.flags, "commit", False)
		try:
			frappe.conf.host_name = "https://demo.example.test"
			frappe.local.response = {}
			frappe.local.flags.commit = False
			with patch.object(ps_module, "reconcile_integration_request", return_value=False):
				payment_success(ir=ir.name, gateway_name=GATEWAY_NAME)
			response = dict(frappe.local.response)
			commit_requested = frappe.local.flags.commit
		finally:
			if original_host_name is None:
				frappe.conf.pop("host_name", None)
			else:
				frappe.conf.host_name = original_host_name
			if original_response is None:
				frappe.local.response = {}
			else:
				frappe.local.response = original_response
			frappe.local.flags.commit = original_commit

		self.assertEqual(response["type"], "redirect")
		self.assertEqual(
			response["location"],
			"https://demo.example.test/payment-failed?doctype=Donation&docname=NPO-DTN-PENDING",
		)
		self.assertFalse(commit_requested)


class TestPayrexxCurrentReadConcurrency(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		if frappe.db.db_type == "sqlite":
			self.skipTest("SQLite does not provide the current row-locking semantics under test")

	def test_concurrent_draft_payment_requests_create_only_one_gateway(self):
		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		frappe.db.rollback()
		settings_name = _ensure_settings()
		frappe.db.commit()
		company = _test_company()
		currency = frappe.db.get_value("Company", company, "default_currency")
		invoice_name = f"PAYREXX-CONCURRENT-SINV-{frappe.generate_hash(length=10)}"
		payment_request_names = [
			f"PAYREXX-CONCURRENT-PR-{frappe.generate_hash(length=10)}" for _index in range(2)
		]
		history_names = [f"PAYREXX-HISTORY-PR-{frappe.generate_hash(length=10)}" for _index in range(2)]
		all_payment_request_names = payment_request_names + history_names
		payment_gateway = f"Payrexx-{settings_name}"
		frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"name": invoice_name,
				"docstatus": 1,
				"is_return": 0,
				"company": company,
				"currency": currency,
				"grand_total": 100,
				"rounded_total": 0,
				"outstanding_amount": 100,
			}
		).db_insert()
		for payment_request_name in payment_request_names:
			frappe.get_doc(
				{
					"doctype": "Payment Request",
					"name": payment_request_name,
					"docstatus": 0,
					"status": "Draft",
					"payment_request_type": "Inward",
					"payment_gateway": payment_gateway,
					"company": company,
					"currency": currency,
					"grand_total": 100,
					"outstanding_amount": 0,
					"reference_doctype": "Sales Invoice",
					"reference_name": invoice_name,
				}
			).db_insert()
		for payment_request_name, docstatus, status in (
			(history_names[0], 1, "Paid"),
			(history_names[1], 2, "Cancelled"),
		):
			frappe.get_doc(
				{
					"doctype": "Payment Request",
					"name": payment_request_name,
					"docstatus": docstatus,
					"status": status,
					"payment_request_type": "Inward",
					"payment_gateway": payment_gateway,
					"company": company,
					"currency": currency,
					"grand_total": 100,
					"outstanding_amount": 0,
					"reference_doctype": "Sales Invoice",
					"reference_name": invoice_name,
				}
			).db_insert()
		frappe.db.commit()

		client = Mock()
		client.create_gateway.return_value = {
			"id": 93001,
			"hash": "concurrent-gateway-hash",
			"link": "https://pay.example/concurrent-checkout",
		}
		barrier = Barrier(2)
		site = frappe.local.site
		sites_path = frappe.local.sites_path
		try:
			with (
				patch.object(ps_module.PayrexxSettings, "_client", return_value=client),
				patch.object(ps_module, "_log_gateway_recovery_pending"),
				patch.object(ps_module, "_log_gateway_recovery_committed"),
				patch.object(ps_module, "_log_gateway_orphan_recovery"),
				ThreadPoolExecutor(max_workers=2) as executor,
			):
				futures = [
					executor.submit(
						_run_concurrent_manual_payrexx_checkout,
						site,
						sites_path,
						settings_name,
						payment_request_name,
						barrier,
					)
					for payment_request_name in payment_request_names
				]
				results = [future.result(timeout=60) for future in futures]

			frappe.db.rollback()
			self.assertEqual(results.count("created"), 1)
			self.assertEqual(results.count("rejected"), 1)
			client.create_gateway.assert_called_once()
			self.assertEqual(
				frappe.db.count(
					"Payment Request",
					{
						"name": ["in", payment_request_names],
						"docstatus": 1,
						"status": "Requested",
					},
				),
				1,
			)
			self.assertEqual(
				frappe.db.count(
					"Integration Request",
					{
						"reference_doctype": "Payment Request",
						"reference_docname": ["in", payment_request_names],
						"integration_request_service": "Payrexx",
					},
				),
				1,
			)
			self.assertTrue(frappe.db.exists("Payment Request", history_names[0]))
			self.assertTrue(frappe.db.exists("Payment Request", history_names[1]))
		finally:
			frappe.db.rollback()
			frappe.db.delete(
				"Integration Request",
				{
					"reference_doctype": "Payment Request",
					"reference_docname": ["in", payment_request_names],
				},
			)
			frappe.db.delete("Payment Request", {"name": ["in", all_payment_request_names]})
			frappe.db.delete("Sales Invoice", {"name": invoice_name})
			frappe.db.commit()

	def test_concurrent_payment_entry_prevents_second_settlement_attempt(self):
		from erpnext.accounts.doctype.payment_request.payment_request import PaymentRequest

		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		payment_request_name = f"PAYREXX-CONCURRENT-PR-{frappe.generate_hash(length=10)}"
		payment_entry_name = f"PAYREXX-CONCURRENT-PE-{frappe.generate_hash(length=10)}"
		integration_request_name = f"PAYREXX-CONCURRENT-IR-{frappe.generate_hash(length=10)}"
		confirmed_transaction = {
			"id": 90001,
			"status": "confirmed",
			"amount": 10000,
			"currency": "CHF",
		}
		with self.primary_connection(), self.secondary_connection():
			frappe.get_doc(
				{
					"doctype": "Payment Request",
					"name": payment_request_name,
					"docstatus": 1,
					"payment_request_type": "Inward",
					"status": "Requested",
					"grand_total": 100,
					"outstanding_amount": 100,
					"currency": "CHF",
				}
			).db_insert()
			frappe.get_doc(
				{
					"doctype": "Integration Request",
					"name": integration_request_name,
					"integration_request_service": "Payrexx",
					"status": "Queued",
					"reference_doctype": "Payment Request",
					"reference_docname": payment_request_name,
					"data": frappe.as_json(
						{
							"payrexx_gateway_amount": 10000,
							"payrexx_gateway_currency": "CHF",
						}
					),
				}
			).db_insert()
			frappe.db.commit()

		try:
			with self.primary_connection():
				frappe.db.rollback()
				stale_payment_request = frappe.get_doc("Payment Request", payment_request_name)
				self.assertEqual(stale_payment_request.status, "Requested")

			with self.primary_connection(), self.secondary_connection():
				frappe.get_doc(
					{
						"doctype": "Payment Entry",
						"name": payment_entry_name,
						"docstatus": 1,
						"payment_type": "Receive",
						"paid_amount": 100,
						"received_amount": 100,
					}
				).db_insert()
				frappe.get_doc(
					{
						"doctype": "Payment Entry Reference",
						"name": frappe.generate_hash(length=10),
						"parent": payment_entry_name,
						"parenttype": "Payment Entry",
						"parentfield": "references",
						"idx": 1,
						"docstatus": 1,
						"reference_doctype": "Sales Invoice",
						"reference_name": "PAYREXX-CONCURRENT-SINV",
						"payment_request": payment_request_name,
						"total_amount": 100,
						"outstanding_amount": 100,
						"allocated_amount": 100,
					}
				).db_insert()
				frappe.db.set_value(
					"Payment Request",
					payment_request_name,
					{"status": "Paid", "outstanding_amount": 0},
					update_modified=False,
				)
				frappe.db.commit()

			with self.primary_connection():
				attempts = []
				complete_locked = ps_module._complete_locked_integration_request

				def count_attempts(request_name, transaction):
					attempts.append((request_name, transaction))
					return complete_locked(request_name, transaction)

				with (
					patch.object(
						ps_module,
						"_complete_locked_integration_request",
						side_effect=count_attempts,
					),
					patch.object(PaymentRequest, "set_as_paid", autospec=True) as settle_again,
					patch.object(ps_module.time, "sleep") as sleep,
				):
					ps_module._complete_integration_request(
						integration_request_name,
						confirmed_transaction,
					)

				self.assertEqual(
					attempts,
					[
						(integration_request_name, confirmed_transaction),
						(integration_request_name, confirmed_transaction),
					],
				)
				sleep.assert_called_once_with(0.25)
				settle_again.assert_not_called()
				current_request = frappe.get_doc("Integration Request", integration_request_name)
				self.assertEqual(current_request.status, "Failed")
				self.assertEqual(
					(frappe.parse_json(current_request.data) or {})[ps_module.SETTLEMENT_CONFLICT_DATA_KEY][
						"code"
					],
					"payment_request_not_active",
				)

			with self.primary_connection(), self.secondary_connection():
				self.assertEqual(
					frappe.db.count(
						"Payment Entry Reference",
						{"payment_request": payment_request_name, "docstatus": 1},
					),
					1,
				)
		finally:
			with self.primary_connection():
				frappe.db.rollback()
			with self.primary_connection(), self.secondary_connection():
				frappe.db.rollback()
				frappe.db.delete(
					"ToDo",
					{
						"reference_type": "Integration Request",
						"reference_name": integration_request_name,
					},
				)
				frappe.db.delete("Payment Entry Reference", {"parent": payment_entry_name})
				frappe.db.delete("Payment Entry", {"name": payment_entry_name})
				frappe.db.delete("Integration Request", {"name": integration_request_name})
				frappe.db.delete("Payment Request", {"name": payment_request_name})
				frappe.db.commit()

	def test_chargeback_boundary_retries_after_concurrent_completion(self):
		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		integration_request_name = f"PAYREXX-CONCURRENT-IR-{frappe.generate_hash(length=10)}"
		confirmed_transaction = {"id": 90501, "status": "confirmed"}
		chargeback_transaction = {"id": 90502, "status": "chargeback"}
		with self.primary_connection(), self.secondary_connection():
			frappe.get_doc(
				{
					"doctype": "Integration Request",
					"name": integration_request_name,
					"integration_request_service": "Payrexx",
					"status": "Queued",
					"data": "{}",
				}
			).db_insert()
			frappe.db.commit()

		try:
			with self.primary_connection():
				stale_request = frappe.get_doc("Integration Request", integration_request_name)
				self.assertEqual(stale_request.status, "Queued")

			with self.primary_connection(), self.secondary_connection():
				frappe.db.set_value(
					"Integration Request",
					integration_request_name,
					{
						"status": "Completed",
						"data": frappe.as_json({"payrexx_transaction": confirmed_transaction}),
					},
					update_modified=False,
				)
				frappe.db.commit()

			with self.primary_connection():
				attempts = []
				mark_locked_chargeback = ps_module._mark_locked_chargeback

				def count_attempts(request_name, transaction=None):
					attempts.append((request_name, transaction))
					return mark_locked_chargeback(request_name, transaction)

				with (
					patch.object(
						ps_module,
						"_mark_locked_chargeback",
						side_effect=count_attempts,
					),
					patch.object(ps_module.time, "sleep") as sleep,
				):
					ps_module._mark_chargeback(integration_request_name, chargeback_transaction)

				self.assertEqual(
					attempts,
					[
						(integration_request_name, chargeback_transaction),
						(integration_request_name, chargeback_transaction),
					],
				)
				sleep.assert_called_once_with(0.25)
				current_request = frappe.get_doc("Integration Request", integration_request_name)
				self.assertEqual(current_request.status, "Failed")
				self.assertEqual(current_request.error, ps_module.CHARGEBACK_ERROR)
				self.assertEqual(
					(frappe.parse_json(current_request.data) or {})["payrexx_transaction"],
					chargeback_transaction,
				)
				self.assertEqual(
					frappe.db.count(
						"ToDo",
						{
							"reference_type": "Integration Request",
							"reference_name": integration_request_name,
							"description": ["like", f"{ps_module.CHARGEBACK_TODO_MARKER}%"],
						},
					),
					1,
				)
		finally:
			with self.primary_connection():
				frappe.db.rollback()
			with self.primary_connection(), self.secondary_connection():
				frappe.db.rollback()
				frappe.db.delete(
					"ToDo",
					{
						"reference_type": "Integration Request",
						"reference_name": integration_request_name,
					},
				)
				frappe.db.delete("Integration Request", {"name": integration_request_name})
				frappe.db.commit()

	def test_reconciliation_failure_preserves_concurrent_terminal_evidence(self):
		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		terminal_cases = (
			(
				"chargeback",
				{"payrexx_transaction": {"id": 91001, "status": "chargeback"}},
				ps_module.CHARGEBACK_ERROR,
			),
			(
				"settlement conflict",
				{
					"payrexx_transaction": {"id": 91002, "status": "confirmed"},
					ps_module.SETTLEMENT_CONFLICT_DATA_KEY: {
						"version": 1,
						"terminal": True,
						"code": "amount_mismatch",
					},
				},
				"Provider amount does not match the requested amount.",
			),
		)

		for label, terminal_data, terminal_error in terminal_cases:
			with self.subTest(terminal_state=label):
				integration_request_name = f"PAYREXX-CONCURRENT-IR-{frappe.generate_hash(length=10)}"
				initial_data = {
					"payrexx_gateway_id": 91000,
					"payrexx_settings": GATEWAY_NAME,
				}
				with self.primary_connection(), self.secondary_connection():
					frappe.get_doc(
						{
							"doctype": "Integration Request",
							"name": integration_request_name,
							"integration_request_service": "Payrexx",
							"status": "Queued",
							"data": frappe.as_json(initial_data),
						}
					).db_insert()
					frappe.db.commit()

				try:
					with self.primary_connection():
						stale_request = frappe.get_doc("Integration Request", integration_request_name)
						self.assertEqual(stale_request.status, "Queued")

					with self.primary_connection(), self.secondary_connection():
						frappe.db.set_value(
							"Integration Request",
							integration_request_name,
							{
								"status": "Failed",
								"error": terminal_error,
								"data": frappe.as_json(terminal_data),
							},
							update_modified=False,
						)
						frappe.db.commit()

					with self.primary_connection():
						client = Mock()
						client.retrieve_gateway.return_value = {
							"status": "declined",
							"invoices": [],
						}
						settings = frappe._dict(_client=lambda: client)
						attempts = []
						reconcile_once = ps_module._reconcile_integration_request_once

						def count_attempts(request_name, gateway_name=None):
							attempts.append((request_name, gateway_name))
							return reconcile_once(request_name, gateway_name)

						with (
							patch.object(ps_module, "_resolve_settings", return_value=settings),
							patch.object(
								ps_module,
								"_reconcile_integration_request_once",
								side_effect=count_attempts,
							),
							patch.object(ps_module.time, "sleep") as sleep,
						):
							self.assertFalse(
								ps_module.reconcile_integration_request(integration_request_name)
							)

						self.assertEqual(
							attempts,
							[
								(integration_request_name, None),
								(integration_request_name, None),
							],
						)
						sleep.assert_called_once_with(0.25)
						client.retrieve_gateway.assert_called_once_with(91000)
						current_request = frappe.get_doc(
							"Integration Request",
							integration_request_name,
							for_update=True,
						)
						self.assertEqual(current_request.status, "Failed")
						self.assertEqual(current_request.error, terminal_error)
						self.assertEqual(frappe.parse_json(current_request.data), terminal_data)
				finally:
					with self.primary_connection():
						frappe.db.rollback()
					with self.primary_connection(), self.secondary_connection():
						frappe.db.rollback()
						frappe.db.delete(
							"Integration Request",
							{"name": integration_request_name},
						)
						frappe.db.commit()

	def test_waiting_callback_observes_concurrently_completed_request(self):
		from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
			payrexx_settings as ps_module,
		)

		integration_request_name = f"PAYREXX-CONCURRENT-IR-{frappe.generate_hash(length=10)}"
		confirmed_transaction = {
			"id": 92001,
			"status": "confirmed",
			"amount": 10000,
			"currency": "CHF",
		}
		with self.primary_connection(), self.secondary_connection():
			frappe.get_doc(
				{
					"doctype": "Integration Request",
					"name": integration_request_name,
					"integration_request_service": "Payrexx",
					"status": "Queued",
					"data": frappe.as_json({"payrexx_settings": GATEWAY_NAME}),
				}
			).db_insert()
			frappe.db.commit()

		try:
			with self.primary_connection():
				_ensure_settings()
				stale_request = frappe.get_doc("Integration Request", integration_request_name)
				self.assertEqual(stale_request.status, "Queued")

			with self.primary_connection(), self.secondary_connection():
				frappe.db.set_value(
					"Integration Request",
					integration_request_name,
					{
						"status": "Completed",
						"data": frappe.as_json(
							{
								"payrexx_settings": GATEWAY_NAME,
								"payrexx_transaction": confirmed_transaction,
							}
						),
					},
					update_modified=False,
				)
				frappe.db.commit()

			waiting_transaction = {
				"id": 92002,
				"status": "waiting",
				"referenceId": integration_request_name,
				"invoice": {"referenceId": integration_request_name},
			}
			body = frappe.as_json({"transaction": waiting_transaction}).encode()
			signature = base64.b64encode(hmac.new(b"whk_test_dummy", body, hashlib.sha256).digest()).decode(
				"ascii"
			)

			class _FakeRequest:
				def __init__(self):
					self.args = {}
					self.form = {}

				def get_data(self):
					return body

			with self.primary_connection():
				original_request = getattr(frappe.local, "request", None)
				frappe.local.request = _FakeRequest()
				attempts = []
				process_callback = ps_module._process_callback_transaction

				def count_attempts(settings_name, transaction, reference_id, status):
					attempts.append((settings_name, transaction, reference_id, status))
					return process_callback(settings_name, transaction, reference_id, status)

				try:
					with (
						patch.object(frappe, "get_request_header", return_value=signature),
						patch.object(
							ps_module,
							"_process_callback_transaction",
							side_effect=count_attempts,
						),
						patch.object(ps_module.time, "sleep") as sleep,
					):
						self.assertEqual(ps_module.callback(gateway_name=GATEWAY_NAME), {"ok": True})
				finally:
					if original_request is None:
						delattr(frappe.local, "request")
					else:
						frappe.local.request = original_request

				self.assertEqual(len(attempts), 2)
				self.assertEqual(
					attempts[0],
					(GATEWAY_NAME, waiting_transaction, integration_request_name, "waiting"),
				)
				self.assertEqual(attempts[1], attempts[0])
				sleep.assert_called_once_with(0.25)
				current_request = frappe.get_doc(
					"Integration Request",
					integration_request_name,
					for_update=True,
				)
				self.assertEqual(current_request.status, "Completed")
				self.assertEqual(
					(frappe.parse_json(current_request.data) or {})["payrexx_transaction"],
					confirmed_transaction,
				)
		finally:
			with self.primary_connection():
				frappe.db.rollback()
			with self.primary_connection(), self.secondary_connection():
				frappe.db.rollback()
				frappe.db.delete("Integration Request", {"name": integration_request_name})
				frappe.db.commit()
