# Copyright (c) 2026, Goodvantage GmbH and contributors
# See license.txt

import base64
import hashlib
import hmac
import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from payrexx_integration.api import _sign, payrexx_pay_url
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
		url = payrexx_pay_url("ACC-SINV-2026-00001")
		self.assertIn("si=ACC-SINV-2026-00001", url)
		self.assertIn("token=", url)
		token = url.split("token=")[1]
		self.assertEqual(len(token), 32)
		# Tampering with the invoice name must invalidate the token.
		self.assertNotEqual(token, _sign("ACC-SINV-2026-00002"))

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
