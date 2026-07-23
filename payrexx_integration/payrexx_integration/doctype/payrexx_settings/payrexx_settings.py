# Copyright (c) 2026, Goodvantage GmbH and contributors
# For license information, please see license.txt

import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, cint, cstr, flt, now_datetime
from payments.utils import create_payment_gateway

from payrexx_integration.gateway_selection import resolve_payrexx_settings
from payrexx_integration.payrexx_integration.payrexx.payrexx_client import PayrexxClient, get_http_status
from payrexx_integration.payrexx_integration.payrexx.webhook_validator import (
	verify_webhook_signature,
)
from payrexx_integration.url_utils import get_public_url, safe_return_url

PAYMENT_AUTHORIZED_MAX_ATTEMPTS = 3
CHARGEBACK_TODO_MARKER = "[Payrexx chargeback]"
SETTLEMENT_CONFLICT_TODO_MARKER = "[Payrexx settlement conflict]"
SETTLEMENT_CONFLICT_DATA_KEY = "payrexx_settlement_conflict"
SETTLEMENT_CONFLICT_VERSION = 1


class PayrexxSettings(Document):
	def validate(self):
		if self.flags.ignore_mandatory or frappe.flags.in_test or frappe.flags.in_install:
			return
		self._ping()

	def on_update(self):
		create_payment_gateway(
			"Payrexx-" + self.gateway_name,
			settings="Payrexx Settings",
			controller=self.gateway_name,
		)
		call_hook_method("payment_gateway_enabled", gateway="Payrexx-" + self.gateway_name)

	def validate_transaction_currency(self, currency):
		currency = cstr(currency).strip().upper()
		if currency not in self._supported_currencies():
			frappe.throw(
				_("Currency {0} is not supported by Payrexx gateway {1}.").format(currency, self.gateway_name)
			)
		try:
			_validate_gateway_currency(currency)
		except ValueError as exc:
			frappe.throw(cstr(exc))

	def get_payment_url(self, **kwargs):
		"""
		Create a Payrexx Gateway and return the hosted checkout URL.

		kwargs originate from a Sales Invoice-backed Payment Request and include:
		  amount, currency, reference_doctype, reference_docname,
		  payer_name, payer_email, description, redirect_to, title
		"""
		self._validate_payment_request_source(kwargs)
		try:
			integration_request = create_request_log(kwargs, service_name="Payrexx")

			payload = self._build_create_gateway_payload(kwargs, integration_request.name)
			gateway = self._client().create_gateway(payload)

			data = frappe.parse_json(integration_request.data) or {}
			data["payrexx_gateway_id"] = gateway.get("id")
			data["payrexx_gateway_hash"] = gateway.get("hash")
			data["payrexx_checkout_url"] = gateway.get("link")
			data["payrexx_gateway_amount"] = payload["amount"]
			data["payrexx_gateway_currency"] = payload["currency"]
			# Authoritative record of which settings row created this request —
			# the webhook only accepts callbacks verified with this row's key.
			data["payrexx_settings"] = self.name
			integration_request.data = frappe.as_json(data)
			integration_request.save(ignore_permissions=True)

			return gateway["link"]
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Payrexx get_payment_url")
			frappe.throw(_("Could not generate Payrexx payment URL"))

	# ------------------------------------------------------------------ helpers

	def _client(self) -> PayrexxClient:
		return PayrexxClient(
			instance=self.instance_name,
			api_secret=self.get_password("api_secret"),
			api_version=self.api_version or "v1.14",
			api_base_domain=self.get("api_base_domain"),
		)

	def _ping(self):
		"""Cheap, side-effect-free credential check.

		Hits ``GET /Gateway/0/`` — Payrexx answers HTTP 200 with
		``{"status":"error","message":"No Gateway found with id 0"}`` when the
		API key + instance are valid. A 401/403 (or unparseable response)
		means the credentials are wrong.
		"""
		client = self._client()
		try:
			body = client.ping_gateway()
		except Exception as exc:
			status_code = get_http_status(exc)
			if status_code in (401, 403):
				frappe.throw(_("Payrexx rejected the API Secret. Check the value in 'API Secret'."))
			if status_code:
				frappe.throw(_("Payrexx returned HTTP {0}").format(status_code))
			frappe.throw(_("Cannot reach Payrexx — check network connectivity."))

		if not isinstance(body, dict) or "status" not in body:
			frappe.throw(
				_(
					"Unexpected response from Payrexx. Check that 'Instance Name' ({0}) and "
					"'API Base Domain' ({1}) are correct."
				).format(client.instance, client.api_base_domain)
			)

	def _supported_currencies(self) -> set[str]:
		raw = self.supported_currencies or "CHF"
		return {c.strip().upper() for c in raw.split(",") if c.strip()}

	def _validate_payment_request_source(self, kwargs: dict) -> None:
		if kwargs.get("reference_doctype") != "Payment Request":
			frappe.throw(_("Payrexx supports only Payment Requests for Sales Invoices."))

		payment_request = frappe.db.get_value(
			"Payment Request",
			kwargs.get("reference_docname"),
			["reference_doctype", "reference_name"],
			as_dict=True,
		)
		if not payment_request:
			frappe.throw(_("Referenced Payment Request was not found."))
		if payment_request.reference_doctype != "Sales Invoice":
			frappe.throw(_("Payrexx supports only Payment Requests for Sales Invoices."))
		if not payment_request.reference_name:
			frappe.throw(_("The Payment Request does not identify a Sales Invoice."))

	def _build_create_gateway_payload(self, kwargs: dict, integration_request_name: str) -> dict:
		currency = cstr(kwargs.get("currency") or "CHF").strip().upper()
		amount_cents = _canonical_gateway_amount(kwargs.get("amount"), currency)
		payer_name = (kwargs.get("payer_name") or "").strip()
		first, last = [*payer_name.split(" ", 1), ""][:2] if payer_name else ("", "")

		payload = {
			"amount": amount_cents,
			"currency": currency,
			"purpose": kwargs.get("description") or kwargs.get("title") or "",
			# referenceId is echoed back on the webhook — use the Integration
			# Request name so we can resolve the in-flight payment in O(1).
			"referenceId": integration_request_name,
			"fields[forename][value]": first,
			"fields[surname][value]": last,
			"fields[email][value]": kwargs.get("payer_email") or "",
			"successRedirectUrl": self._return_url(kwargs, "success", integration_request_name),
			"failedRedirectUrl": self._return_url(kwargs, "failed"),
			"cancelRedirectUrl": self._return_url(kwargs, "cancel"),
		}

		if self.validity_minutes:
			payload["validity"] = int(self.validity_minutes)

		if self.psp:
			for i, pid in enumerate(p.strip() for p in self.psp.split(",") if p.strip()):
				payload[f"psp[{i}]"] = int(pid)

		return payload

	def _return_url(self, kwargs: dict, kind: str, integration_request_name: str | None = None) -> str:
		request_redirect = kwargs.get(f"{kind}_redirect_to") or kwargs.get(f"{kind}_redirect_url")
		if request_redirect:
			return safe_return_url(request_redirect, error_label="Unsafe Payrexx return URL")

		override = {
			"success": self.success_redirect_url,
			"failed": self.failed_redirect_url,
			"cancel": self.cancel_redirect_url,
		}.get(kind)
		if override:
			return override

		if kind == "success" and integration_request_name:
			return get_public_url(
				"/api/method/payrexx_integration.api.payment_success?"
				+ urlencode(
					{
						"ir": integration_request_name,
						"gateway_name": self.name,
					}
				)
			)

		base = get_public_url("/payment-success" if kind == "success" else "/payment-failed")
		params = {
			"doctype": kwargs.get("reference_doctype", ""),
			"docname": kwargs.get("reference_docname", ""),
		}
		if kwargs.get("redirect_to"):
			params["redirect_to"] = kwargs["redirect_to"]
		return f"{base}?{urlencode(params)}"


# =============================================================================
# Webhook
# =============================================================================


@frappe.whitelist()
def get_webhook_url(gateway_name: str | None = None) -> str:
	gateway_name = cstr(gateway_name).strip()
	if not gateway_name:
		return ""

	return get_public_url(
		"/api/method/payrexx_integration.payrexx_integration.doctype.payrexx_settings."
		"payrexx_settings.callback?" + urlencode({"gateway_name": gateway_name})
	)


@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep: guest-whitelisted-method
def callback(gateway_name: str | None = None) -> dict[str, bool]:
	"""
	Configure the following URL in Payrexx -> Webhooks for each gateway:

	  https://<your-site>/api/method/payrexx_integration.payrexx_integration.\
doctype.payrexx_settings.payrexx_settings.callback?gateway_name=Live

	The gateway_name query param is required when more than one Payrexx Settings
	row exists, so we know which signing key to verify against.
	"""
	try:
		raw_body = frappe.request.get_data() or b""
		signature = frappe.get_request_header("X-Webhook-Signature", "")

		settings = _resolve_settings(_gateway_name_from_request(gateway_name))
		if not verify_webhook_signature(raw_body, signature, settings.get_password("webhook_signing_key")):
			frappe.throw(_("Invalid Payrexx webhook signature"), frappe.AuthenticationError)

		body = frappe.parse_json(raw_body.decode("utf-8") if raw_body else "{}") or {}
		txn = body.get("transaction") or {}
		invoice = txn.get("invoice") or {}
		ref_id = invoice.get("referenceId") or txn.get("referenceId")
		status = (txn.get("status") or "").lower()

		if not ref_id:
			frappe.log_error(
				frappe.as_json(_webhook_log_summary(txn, ref_id, status)),
				"Payrexx webhook missing referenceId",
			)
			return {"ok": True}

		if not frappe.db.exists("Integration Request", ref_id):
			frappe.log_error(
				frappe.as_json(_webhook_log_summary(txn, ref_id, status)),
				"Payrexx webhook unknown reference",
			)
			return {"ok": True}

		ir = frappe.get_doc("Integration Request", ref_id)
		if ir.integration_request_service != "Payrexx":
			frappe.log_error(
				frappe.as_json(_webhook_log_summary(txn, ref_id, status)),
				"Payrexx webhook wrong Integration Request service",
			)
			return {"ok": True}

		ir_data = frappe.parse_json(ir.data) or {}

		# Bind the verifying key to the Integration Request's own gateway: a
		# webhook signed with one row's key (e.g. Sandbox) must not complete a
		# request created by another row (e.g. Live).
		expected_settings = ir_data.get("payrexx_settings") or _settings_name_from_request_data(ir_data)
		if expected_settings and expected_settings != settings.name:
			frappe.log_error(
				frappe.as_json(
					_webhook_log_summary(txn, ref_id, status)
					| {"verified_with": settings.name, "expected": expected_settings}
				),
				"Payrexx webhook gateway mismatch",
			)
			return {"ok": True}

		# A confirmed settlement conflict is an accounting terminal state. Keep
		# authenticating replays, but never let a later status silently reopen it.
		if ir_data.get(SETTLEMENT_CONFLICT_DATA_KEY):
			if status == "chargeback":
				_mark_chargeback(ir.name, txn)
			return {"ok": True}

		if status == "confirmed":
			_complete_integration_request(ir.name, txn)
		elif status in ("authorized", "reserved"):
			ir_data["payrexx_transaction"] = txn
			ir.data = frappe.as_json(ir_data)
			ir.status = "Authorized"
			ir.save(ignore_permissions=True)
		elif status == "chargeback":
			_mark_chargeback(ir.name, txn)
		elif status in ("cancelled", "declined", "error", "expired"):
			ir_data["payrexx_transaction"] = txn
			ir.data = frappe.as_json(ir_data)
			ir.status = "Failed"
			ir.error = f"Payrexx status: {status}"
			ir.save(ignore_permissions=True)
		else:
			# 'waiting' and anything we don't recognise — keep listening.
			ir_data["payrexx_transaction"] = txn
			ir.data = frappe.as_json(ir_data)
			ir.save(ignore_permissions=True)

		return {"ok": True}
	except frappe.AuthenticationError:
		raise
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Payrexx callback error")
		raise


_resolve_settings = resolve_payrexx_settings


def _gateway_name_from_request(gateway_name: str | None) -> str | None:
	if gateway_name:
		return cstr(gateway_name).strip()

	request = getattr(frappe.local, "request", None)
	request_args = getattr(request, "args", None) if request else None
	if request_args:
		request_gateway_name = request_args.get("gateway_name")
		if request_gateway_name:
			return cstr(request_gateway_name).strip()

	form_dict = getattr(frappe.local, "form_dict", None)
	if form_dict:
		form_gateway_name = form_dict.get("gateway_name")
		if form_gateway_name:
			return cstr(form_gateway_name).strip()

	return None


def _webhook_log_summary(txn: dict, ref_id: str | None, status: str | None) -> dict[str, str | int | None]:
	invoice = txn.get("invoice") or {}
	instance = txn.get("instance") or {}
	return {
		"reference_id": ref_id,
		"status": status or txn.get("status"),
		"transaction_id": txn.get("id"),
		"transaction_uuid": txn.get("uuid"),
		"mode": txn.get("mode"),
		"instance_name": instance.get("name"),
		"payment_request_id": invoice.get("paymentRequestId"),
	}


def reconcile_integration_request(
	integration_request_name: str,
	gateway_name: str | None = None,
) -> bool:
	"""Confirm an Integration Request by asking Payrexx for the Gateway status.

	This is a fallback for success redirects. Webhooks remain the primary source
	of truth, but the browser return can safely reconcile because the server
	checks Payrexx before applying payment side effects.
	"""
	if not integration_request_name or not frappe.db.exists("Integration Request", integration_request_name):
		return False

	ir = frappe.get_doc("Integration Request", integration_request_name)
	if ir.integration_request_service != "Payrexx":
		return False
	ir_data = frappe.parse_json(ir.data) or {}
	if ir_data.get(SETTLEMENT_CONFLICT_DATA_KEY):
		return False
	if ir.status == "Completed":
		stored_transaction = ir_data.get("payrexx_transaction") or {}
		return (stored_transaction.get("status") or "").lower() == "confirmed"
	gateway_id = ir_data.get("payrexx_gateway_id")
	if not gateway_id:
		return False

	# The Integration Request's own gateway decides which credentials confirm
	# the payment; the caller-supplied gateway_name is only a legacy fallback
	# for requests that predate the stored gateway reference.
	settings = _resolve_settings(
		ir_data.get("payrexx_settings") or _settings_name_from_request_data(ir_data) or gateway_name
	)
	gateway = settings._client().retrieve_gateway(int(gateway_id))
	transaction = _confirmed_transaction_from_gateway(gateway)

	if transaction:
		_complete_integration_request(ir.name, transaction)
		return frappe.db.get_value("Integration Request", ir.name, "status") == "Completed"
	status = (gateway.get("status") or "").lower()
	if status == "chargeback":
		_mark_chargeback(ir.name, gateway)
	elif status in ("cancelled", "declined", "error", "expired"):
		ir.status = "Failed"
		ir.error = f"Payrexx status: {status}"
		ir.save(ignore_permissions=True)
	return False


def _settings_name_from_request_data(ir_data: dict) -> str | None:
	payment_gateway = (ir_data.get("payment_gateway") or "").strip()
	if payment_gateway.startswith("Payrexx-"):
		return payment_gateway.removeprefix("Payrexx-")
	return None


def _confirmed_transaction_from_gateway(gateway: dict) -> dict:
	for invoice in gateway.get("invoices") or []:
		for transaction in invoice.get("transactions") or []:
			if (transaction.get("status") or "").lower() == "confirmed":
				amount = transaction.get("amount")
				if amount is None:
					amount = invoice.get("amount", gateway.get("amount"))
				return {
					**transaction,
					"amount": amount,
					"currency": transaction.get("currency")
					or invoice.get("currency")
					or gateway.get("currency"),
				}
	return {}


def _complete_integration_request(integration_request_name: str, transaction: dict | None = None) -> None:
	"""Atomically record confirmation and settle its reference, retrying the whole unit."""
	for attempt in range(1, PAYMENT_AUTHORIZED_MAX_ATTEMPTS + 1):
		try:
			_complete_locked_integration_request(integration_request_name, transaction)
			return
		except frappe.QueryDeadlockError:
			frappe.db.rollback()
			if attempt == PAYMENT_AUTHORIZED_MAX_ATTEMPTS:
				raise
			time.sleep(0.25 * attempt)


def _complete_locked_integration_request(
	integration_request_name: str, transaction: dict | None = None
) -> None:
	frappe.db.get_value("Integration Request", integration_request_name, "name", for_update=True)
	integration_request = frappe.get_doc("Integration Request", integration_request_name)
	ir_data = frappe.parse_json(integration_request.data) or {}
	previous_transaction = ir_data.get("payrexx_transaction") or {}
	if (previous_transaction.get("status") or "").lower() == "chargeback":
		return
	if ir_data.get(SETTLEMENT_CONFLICT_DATA_KEY):
		return
	if integration_request.status == "Completed":
		return

	if transaction:
		ir_data["payrexx_transaction"] = transaction
	if conflict := _settlement_conflict(integration_request, ir_data, transaction or {}):
		_mark_settlement_conflict(integration_request, ir_data, conflict)
		return
	integration_request.data = frappe.as_json(ir_data)
	integration_request.status = "Completed"
	integration_request.error = ""
	integration_request.save(ignore_permissions=True)
	payment_entry_name = _on_payment_authorized(integration_request, "Completed")
	if payment_entry_name:
		ir_data["payrexx_payment_entry"] = payment_entry_name
		integration_request.db_set("data", frappe.as_json(ir_data), update_modified=False)


def _on_payment_authorized(integration_request, status) -> str | None:
	if not (integration_request.reference_doctype and integration_request.reference_docname):
		return None
	try:
		with _payment_authorization_user():
			if integration_request.reference_doctype == "Payment Request":
				return _set_payment_request_as_paid(integration_request.reference_docname)
			else:
				frappe.get_doc(
					integration_request.reference_doctype,
					integration_request.reference_docname,
				).run_method("on_payment_authorized", status)
	except frappe.QueryDeadlockError:
		raise
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Payrexx on_payment_authorized")
		raise
	return None


def _set_payment_request_as_paid(payment_request_name: str) -> str | None:
	frappe.db.get_value("Payment Request", payment_request_name, "name", for_update=True)
	payment_request = frappe.get_doc("Payment Request", payment_request_name)
	if payment_request.status == "Paid" or flt(payment_request.outstanding_amount) <= 0:
		return None
	payment_entry = payment_request.set_as_paid()
	return payment_entry.name if payment_entry else None


def _canonical_gateway_amount(amount, currency: str) -> int:
	"""Convert a two-decimal checkout amount to Payrexx's canonical integer unit."""
	currency = cstr(currency).strip().upper()
	_validate_gateway_currency(currency)
	try:
		decimal_amount = Decimal(str(amount))
	except (InvalidOperation, TypeError, ValueError):
		raise ValueError(_("Payment amount is invalid."))
	if not decimal_amount.is_finite() or decimal_amount <= 0:
		raise ValueError(_("Payment amount must be a positive finite number."))
	scaled_amount = decimal_amount * 100
	if scaled_amount != scaled_amount.to_integral_value():
		raise ValueError(_("Payment amount has precision smaller than the supported currency unit."))
	return int(scaled_amount)


def _validate_gateway_currency(currency: str) -> None:
	fraction_units = frappe.db.get_value("Currency", currency, "fraction_units")
	if cint(fraction_units) != 100:
		raise ValueError(
			_("Currency {0} does not have a supported two-decimal fraction unit.").format(currency or "?")
		)


def _provider_gateway_amount(amount) -> int:
	try:
		decimal_amount = Decimal(str(amount))
	except (InvalidOperation, TypeError, ValueError):
		raise ValueError(_("Provider amount is invalid."))
	if not decimal_amount.is_finite() or decimal_amount != decimal_amount.to_integral_value():
		raise ValueError(_("Provider amount must be an integer in the smallest currency unit."))
	return int(decimal_amount)


def _conflict(code: str, reason: str, evidence: dict | None = None) -> dict:
	return {"code": code, "reason": reason, "evidence": evidence or {}}


def _settlement_conflict(integration_request, ir_data: dict, transaction: dict) -> dict | None:
	provider_amount = transaction.get("amount")
	provider_currency = transaction.get("currency")
	provider_invoice = transaction.get("invoice") or {}
	if provider_amount is None:
		provider_amount = provider_invoice.get("amount")
	if not provider_currency:
		provider_currency = provider_invoice.get("currency")

	expected_amount = ir_data.get("payrexx_gateway_amount")
	expected_currency = ir_data.get("payrexx_gateway_currency") or ir_data.get("currency")
	evidence = {
		"expected_gateway_amount": expected_amount,
		"expected_currency": cstr(expected_currency).strip().upper() or None,
		"provider_amount": provider_amount,
		"provider_currency": cstr(provider_currency).strip().upper() or None,
	}
	if provider_amount is None or not provider_currency:
		return _conflict(
			"provider_evidence_missing",
			_("Provider confirmation does not contain an amount and currency."),
			evidence,
		)
	if not expected_currency:
		return _conflict(
			"checkout_evidence_missing",
			_("The original payment request does not contain an amount and currency."),
			evidence,
		)
	try:
		provider_integer = _provider_gateway_amount(provider_amount)
		if expected_amount is None:
			expected_integer = _canonical_gateway_amount(ir_data.get("amount"), expected_currency)
		else:
			expected_integer = _provider_gateway_amount(expected_amount)
	except ValueError as exc:
		return _conflict("invalid_amount_or_currency", cstr(exc), evidence)
	evidence["expected_gateway_amount"] = expected_integer
	evidence["provider_amount"] = provider_integer
	if provider_integer != expected_integer:
		return _conflict(
			"amount_mismatch",
			_("Provider amount does not match the requested amount."),
			evidence,
		)
	if cstr(provider_currency).upper() != cstr(expected_currency).upper():
		return _conflict(
			"currency_mismatch",
			_("Provider currency does not match the requested currency."),
			evidence,
		)

	if (
		integration_request.reference_doctype != "Payment Request"
		or not integration_request.reference_docname
	):
		return _conflict(
			"payment_request_reference_required",
			_("A confirmed Payrexx checkout must reference a Payment Request."),
			evidence,
		)
	if not frappe.db.exists("Payment Request", integration_request.reference_docname):
		return _conflict(
			"payment_request_missing",
			_("The referenced Payment Request no longer exists."),
			evidence,
		)
	frappe.db.get_value("Payment Request", integration_request.reference_docname, "name", for_update=True)
	payment_request = frappe.get_doc("Payment Request", integration_request.reference_docname)
	evidence["payment_request"] = {
		"name": payment_request.name,
		"docstatus": payment_request.docstatus,
		"status": payment_request.status,
		"payment_request_type": payment_request.payment_request_type,
		"grand_total": payment_request.grand_total,
		"outstanding_amount": payment_request.outstanding_amount,
		"currency": payment_request.currency,
		"reference_doctype": payment_request.reference_doctype,
		"reference_name": payment_request.reference_name,
	}
	if payment_request.docstatus != 1 or payment_request.status != "Requested":
		return _conflict(
			"payment_request_not_active",
			_("The referenced Payment Request is not active and submitted."),
			evidence,
		)
	if payment_request.payment_request_type != "Inward":
		return _conflict(
			"payment_request_not_inward",
			_("Only an inward Payment Request can be settled by Payrexx."),
			evidence,
		)
	if not payment_request.reference_doctype or not payment_request.reference_name:
		return _conflict(
			"source_reference_missing",
			_("The Payment Request does not identify a source document."),
			evidence,
		)
	if payment_request.reference_doctype != "Sales Invoice":
		return _conflict(
			"unsupported_source_doctype",
			_("Payrexx supports only Payment Requests for Sales Invoices."),
			evidence,
		)
	if not frappe.db.exists(payment_request.reference_doctype, payment_request.reference_name):
		return _conflict(
			"source_document_missing",
			_("The Payment Request source document no longer exists."),
			evidence,
		)
	frappe.db.get_value(
		payment_request.reference_doctype,
		payment_request.reference_name,
		"name",
		for_update=True,
	)
	reference_document = frappe.get_doc(
		payment_request.reference_doctype,
		payment_request.reference_name,
	)
	reference_currency = cstr(reference_document.get("currency")).strip().upper()
	reference_outstanding = reference_document.get("outstanding_amount")
	evidence["reference_document"] = {
		"doctype": reference_document.doctype,
		"name": reference_document.name,
		"docstatus": reference_document.docstatus,
		"currency": reference_currency or None,
		"outstanding_amount": reference_outstanding,
	}
	if reference_document.docstatus != 1:
		return _conflict(
			"source_document_not_submitted",
			_("The Payment Request source document is not submitted."),
			evidence,
		)

	payment_currency = cstr(payment_request.currency).strip().upper()
	party_account_currency = cstr(payment_request.get("party_account_currency")).strip().upper()
	payment_account_currency = ""
	if payment_request.payment_account:
		payment_account_currency = (
			cstr(frappe.get_cached_value("Account", payment_request.payment_account, "account_currency"))
			.strip()
			.upper()
		)
	evidence["payment_request"]["party_account_currency"] = party_account_currency or None
	evidence["payment_request"]["payment_account_currency"] = payment_account_currency or None
	if (
		payment_currency != cstr(expected_currency).strip().upper()
		or not reference_currency
		or reference_currency != payment_currency
		or (party_account_currency and party_account_currency != payment_currency)
		or (payment_account_currency and payment_account_currency != payment_currency)
	):
		return _conflict(
			"unsupported_currency_context",
			_("The Payment Request uses a foreign-currency settlement path that Payrexx cannot safely post."),
			evidence,
		)
	try:
		payment_request_gateway_amount = _canonical_gateway_amount(
			payment_request.grand_total,
			payment_currency,
		)
	except ValueError as exc:
		return _conflict("invalid_payment_request_amount", cstr(exc), evidence)
	if payment_request_gateway_amount != expected_integer:
		return _conflict(
			"payment_request_amount_changed",
			_("The Payment Request amount changed after the Payrexx checkout was created."),
			evidence,
		)

	precision = payment_request.precision("outstanding_amount")
	if flt(payment_request.outstanding_amount, precision) != flt(payment_request.grand_total, precision):
		return _conflict(
			"payment_request_already_changed",
			_("The payment request was already settled or changed through another payment channel."),
			evidence,
		)
	if reference_outstanding is not None and flt(reference_outstanding, precision) <= 0:
		return _conflict(
			"source_document_already_settled",
			_("The receivable was already settled through another payment channel."),
			evidence,
		)
	if reference_outstanding is not None and flt(reference_outstanding, precision) < flt(
		payment_request.grand_total, precision
	):
		return _conflict(
			"source_document_partly_settled",
			_("The receivable was partly settled through another payment channel."),
			evidence,
		)
	return None


def _mark_settlement_conflict(integration_request, ir_data: dict, conflict: dict) -> None:
	if ir_data.get(SETTLEMENT_CONFLICT_DATA_KEY):
		return
	marker = {
		"version": SETTLEMENT_CONFLICT_VERSION,
		"terminal": True,
		"detected_at": now_datetime(),
		**conflict,
	}
	ir_data[SETTLEMENT_CONFLICT_DATA_KEY] = marker
	integration_request.data = frappe.as_json(ir_data)
	integration_request.status = "Failed"
	integration_request.error = conflict["reason"]
	integration_request.save(ignore_permissions=True)
	with _payment_authorization_user():
		if frappe.db.exists(
			"ToDo",
			{
				"reference_type": "Integration Request",
				"reference_name": integration_request.name,
				"description": ["like", f"{SETTLEMENT_CONFLICT_TODO_MARKER}%"],
			},
		):
			return
		frappe.get_doc(
			{
				"doctype": "ToDo",
				"status": "Open",
				"priority": "High",
				"allocated_to": frappe.session.user,
				"assigned_by": frappe.session.user,
				"reference_type": "Integration Request",
				"reference_name": integration_request.name,
				"description": f"{SETTLEMENT_CONFLICT_TODO_MARKER} {conflict['reason']}",
			}
		).insert(ignore_permissions=True)


def _mark_chargeback(integration_request_name: str, transaction: dict | None = None) -> None:
	frappe.db.get_value("Integration Request", integration_request_name, "name", for_update=True)
	integration_request = frappe.get_doc("Integration Request", integration_request_name)
	ir_data = frappe.parse_json(integration_request.data) or {}
	if transaction:
		ir_data["payrexx_transaction"] = transaction
	integration_request.db_set(
		{
			"data": frappe.as_json(ir_data),
			"status": "Failed",
			"error": "Payrexx status: chargeback",
		}
	)

	with _payment_authorization_user():
		if frappe.db.exists(
			"ToDo",
			{
				"reference_type": "Integration Request",
				"reference_name": integration_request.name,
				"description": ["like", f"{CHARGEBACK_TODO_MARKER}%"],
			},
		):
			return
		frappe.get_doc(
			{
				"doctype": "ToDo",
				"status": "Open",
				"priority": "High",
				"allocated_to": frappe.session.user,
				"assigned_by": frappe.session.user,
				"reference_type": "Integration Request",
				"reference_name": integration_request.name,
				"description": f"{CHARGEBACK_TODO_MARKER} "
				+ _(
					"Manual accounting reversal required. Review the linked settlement; "
					"submitted ledger records were preserved."
				),
			}
		).insert(ignore_permissions=True)


def _payment_authorization_user():
	from payrexx_integration.session_utils import as_automation_user

	return as_automation_user()
