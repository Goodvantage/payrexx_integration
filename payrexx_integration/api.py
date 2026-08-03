# Copyright (c) 2026, Goodvantage GmbH and contributors

"""Public, whitelisted entry points for the Payrexx integration.

The main one is :func:`pay_invoice` — a stable redirect URL we can embed in
"pay later" emails. Each click lazy-creates a Payrexx Gateway via a Payment
Request linked to the Sales Invoice and 302s the customer to the hosted
checkout. The URL never goes stale because nothing about the Payrexx side is
baked into the link itself.

The link is HMAC-signed so an attacker can't enumerate invoice names and pay
on someone else's behalf (or trigger arbitrary Payment Request creation).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

import frappe
from frappe import _

from payrexx_integration.gateway_selection import resolve_payrexx_settings
from payrexx_integration.payrexx_integration.doctype.payrexx_settings.payrexx_settings import (
	CHECKOUT_PROVIDER_CONTACT_FLAG,
	DEADLOCK_MAX_ATTEMPTS,
	PAYREXX_SUCCESS_TOKEN_VERSION,
	PAYREXX_SUCCESS_TOKEN_VERSION_KEY,
	_canonical_gateway_amount,
	_get_active_payrexx_payment_requests,
	_provider_gateway_amount,
	_validate_payment_request_checkout_state,
	_validate_sales_invoice_checkout_state,
)
from payrexx_integration.session_utils import as_automation_user
from payrexx_integration.url_utils import get_public_url
from payrexx_integration.url_utils import safe_return_url as _safe_return_url

# ---------------------------------------------------------------- token helpers


def _signing_key() -> bytes:
	"""Per-site secret used to sign pay-by-email links.

	Reuses the site's ``encryption_key`` so we don't introduce yet another
	secret to manage. The key is in ``site_config.json`` and is required for
	a Frappe site to boot, so it's always present.
	"""
	key = frappe.local.conf.get("encryption_key")
	if not key:
		frappe.throw(_("Site encryption_key is not configured"))
	return key.encode("utf-8") if isinstance(key, str) else bytes(key)


def sign_reference(payload: str) -> str:
	"""HMAC-sign an arbitrary reference string with the site key.

	Shared signer for compact email-link tokens (pay-by-email here, the dummy
	checkout in good_demo). Callers compose the payload string; keep existing
	compositions stable so in-flight links keep verifying.
	"""
	digest = hmac.new(_signing_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
	# 32 hex chars = 128 bits of HMAC output, enough to make guessing infeasible
	# while keeping the URL compact in an email.
	return digest[:32]


def verify_reference(payload: str, token: str | None) -> bool:
	if not (payload and token):
		return False
	return hmac.compare_digest(sign_reference(payload), str(token))


def _sign(invoice_name: str, gateway_name: str | None = None) -> str:
	payload = invoice_name if not gateway_name else f"{invoice_name}|{gateway_name}"
	return sign_reference(payload)


def _verify(invoice_name: str, token: str, gateway_name: str | None = None) -> bool:
	if not (invoice_name and token):
		return False
	if gateway_name:
		return verify_reference(f"{invoice_name}|{gateway_name}", token)
	# Backward compatible for links generated before gateway_name was included.
	return verify_reference(invoice_name, token)


def sign_payment_success_reference(integration_request: str, gateway_name: str) -> str:
	return sign_reference(f"{integration_request}|{gateway_name}|payment_success")


def _verify_payment_success_reference(
	integration_request: str | None,
	gateway_name: str | None,
	token: str | None,
) -> bool:
	if not (integration_request and gateway_name and token):
		return False
	return verify_reference(f"{integration_request}|{gateway_name}|payment_success", token)


# ---------------------------------------------------------------- jinja helper


def payrexx_pay_url(sales_invoice: str | None, gateway_name: str | None = None) -> str:
	"""Return the public pay-by-email URL for a Sales Invoice.

	Registered as a jinja method via ``hooks.py`` — call from any email
	template as ``{{ payrexx_pay_url(doc.name) }}``.

	Returns an empty string when called with no invoice so jinja templates
	can guard with ``{% if payrexx_pay_url(...) %}`` cleanly.
	"""
	if not sales_invoice:
		return ""
	if not frappe.db.exists("Sales Invoice", sales_invoice):
		return ""
	if frappe.db.get_value("Sales Invoice", sales_invoice, "docstatus") != 1:
		return ""
	try:
		settings_name = resolve_payrexx_settings(gateway_name).name
	except Exception:
		frappe.log_error(title="Payrexx pay URL unavailable", message=frappe.get_traceback())
		return ""
	params = {
		"si": sales_invoice,
		"gateway_name": settings_name,
		"token": _sign(sales_invoice, settings_name),
	}
	return get_public_url("/api/method/payrexx_integration.api.pay_invoice?" + urlencode(params))


def safe_pay_url(sales_invoice: str | None, gateway_name: str | None = None) -> str:
	"""Never-raise variant of :func:`payrexx_pay_url` for email/print rendering.

	Consumer apps embed pay links while composing invoice and dunning
	output; a Payrexx misconfiguration must degrade to "no link", never
	break the document. This wrapper owns that fallback contract in one
	place instead of each app hand-rolling its own try/except.
	"""
	try:
		return payrexx_pay_url(sales_invoice, gateway_name) or ""
	except Exception:
		frappe.log_error(title="Payrexx pay URL unavailable", message=frappe.get_traceback())
		return ""


# -------------------------------------------------------------- redirect entry


@frappe.whitelist(allow_guest=True, methods=["GET"])  # nosemgrep: guest-whitelisted-method
def pay_invoice(si: str | None = None, token: str | None = None, gateway_name: str | None = None) -> None:
	"""Lazy-create a Payrexx Gateway for a Sales Invoice and redirect to it.

	Embedded in pay-by-email links via :func:`payrexx_pay_url`. The signed
	token authorises the caller — without it, any URL is rejected.
	"""
	if not _verify(si, token, gateway_name):
		frappe.throw(_("Invalid or expired payment link"), frappe.PermissionError)

	if not frappe.db.exists("Sales Invoice", si):
		frappe.throw(_("Invoice not found"), frappe.DoesNotExistError)

	sales_invoice = frappe.get_doc("Sales Invoice", si)
	if sales_invoice.docstatus == 2:
		frappe.throw(_("This invoice has been cancelled"))
	if sales_invoice.docstatus != 1:
		frappe.throw(_("This invoice is not submitted"))

	# Resolve before the paid-invoice shortcut so legacy gateway-unbound links
	# retain the same strict ambiguity contract on every successful path.
	settings_name = resolve_payrexx_settings(gateway_name).name
	if sales_invoice.outstanding_amount is not None and sales_invoice.outstanding_amount <= 0:
		# Already paid — send the customer to the success page instead of
		# creating a duplicate Payrexx gateway.
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = get_public_url(
			"/payment-success?doctype=Sales Invoice&docname=" + si
		)
		return

	# Re-use an existing Payment Request for this invoice if one is still
	# pending, otherwise create a fresh one. This keeps the Payment Request
	# log clean when a customer clicks the email link multiple times.
	# Runs as the configured least-privilege automation user (same resolution
	# as the webhook path), never as a hardcoded Administrator.
	with as_automation_user(settings_name):
		checkout_url = _run_checkout_with_deadlock_retry(lambda: _get_invoice_checkout_url(si, settings_name))

	# Email links must remain GET requests, but successful first-click setup writes
	# the Payment Request and Integration Request. Frappe rolls back GET requests
	# unless this framework flag is set; set it only after the checkout is valid.
	frappe.local.flags.commit = True
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = checkout_url


@frappe.whitelist(allow_guest=True, methods=["GET"])  # nosemgrep: guest-whitelisted-method
def payment_success(
	ir: str | None = None,
	gateway_name: str | None = None,
	token: str | None = None,
) -> None:
	"""Reconcile a Payrexx success redirect, then send the customer to the final return URL."""
	if token and not _verify_payment_success_reference(ir, gateway_name, token):
		frappe.throw(_("Invalid payment return"), frappe.PermissionError)
	if not ir:
		frappe.throw(_("Invalid payment return"), frappe.PermissionError)

	try:
		integration_request = frappe.get_doc("Integration Request", ir)
	except frappe.DoesNotExistError:
		frappe.throw(_("Invalid payment return"), frappe.PermissionError)
	if integration_request.integration_request_service != "Payrexx":
		frappe.throw(_("Invalid payment return"), frappe.PermissionError)

	ir_data = frappe.parse_json(integration_request.data) or {}
	if PAYREXX_SUCCESS_TOKEN_VERSION_KEY in ir_data:
		marker_version = ir_data[PAYREXX_SUCCESS_TOKEN_VERSION_KEY]
		if (
			not isinstance(marker_version, int)
			or isinstance(marker_version, bool)
			or marker_version != PAYREXX_SUCCESS_TOKEN_VERSION
		):
			frappe.throw(_("Invalid payment return"), frappe.PermissionError)
		stored_gateway = ir_data.get("payrexx_settings")
		if not token or not stored_gateway or stored_gateway != gateway_name:
			frappe.throw(_("Invalid payment return"), frappe.PermissionError)

	from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
		payrexx_settings,
	)

	reconciled = payrexx_settings.reconcile_integration_request(ir, gateway_name=gateway_name)
	integration_request.reload()
	if reconciled or integration_request.status == "Failed":
		# Provider returns use GET, so Frappe would otherwise roll back the
		# server-verified settlement or terminal provider status after redirecting.
		frappe.local.flags.commit = True
	if not reconciled:
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = _payment_failed_redirect_url(integration_request)
		return
	redirect_url = _payment_success_redirect_url(integration_request)
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = redirect_url


# ------------------------------------------------------------------- internals


class _CheckoutLockOrderRetry(Exception):
	pass


def _run_checkout_with_deadlock_retry(operation):
	"""Replay the complete checkout boundary only while no provider POST occurred."""
	for attempt in range(1, DEADLOCK_MAX_ATTEMPTS + 1):
		frappe.flags[CHECKOUT_PROVIDER_CONTACT_FLAG] = False
		try:
			return operation()
		except _CheckoutLockOrderRetry:
			provider_contacted = bool(frappe.flags.get(CHECKOUT_PROVIDER_CONTACT_FLAG))
			frappe.db.rollback()
			if provider_contacted:
				raise
			if attempt == DEADLOCK_MAX_ATTEMPTS:
				frappe.throw(
					_("Payment state changed repeatedly while checkout was prepared. Please retry the link.")
				)
		except frappe.QueryDeadlockError:
			provider_contacted = bool(frappe.flags.get(CHECKOUT_PROVIDER_CONTACT_FLAG))
			frappe.db.rollback()
			if provider_contacted or attempt == DEADLOCK_MAX_ATTEMPTS:
				raise
		if attempt < DEADLOCK_MAX_ATTEMPTS:
			time.sleep(0.25 * attempt)


def _get_invoice_checkout_url(sales_invoice_name: str, settings_name: str) -> str:
	sales_invoice = frappe.get_doc("Sales Invoice", sales_invoice_name)
	payment_request, sales_invoice = _get_or_create_payment_request(sales_invoice, settings_name)
	return _get_payment_request_checkout_url(payment_request, sales_invoice, settings_name)


def _get_or_create_payment_request(sales_invoice, settings_name: str):
	_validate_sales_invoice_checkout_state(sales_invoice)
	gateway = "Payrexx-" + settings_name
	existing = _get_active_payrexx_payment_requests(sales_invoice.name, for_update=False)
	if len(existing) > 1:
		frappe.throw(
			_(
				"Multiple active Payrexx Payment Requests exist for this invoice. "
				"The accounts team must review them before online payment can continue."
			)
		)
	if existing:
		if existing[0].payment_gateway != gateway:
			frappe.throw(
				_(
					"Another active Payrexx Payment Request already exists for this invoice. "
					"The accounts team must review it before online payment can continue."
				)
			)
		# Do not lock the source or request before the active Integration Request.
		# The reuse helper acquires the settlement-compatible order.
		return frappe._dict(name=existing[0].name), frappe._dict(name=sales_invoice.name)

	# No active request was visible. Serialize first creation on the invoice and
	# repeat the query as a current locking read. If a request won while this
	# transaction waited, restart so its Integration Request can be locked first.
	sales_invoice = frappe.get_doc("Sales Invoice", sales_invoice.name, for_update=True)
	_validate_sales_invoice_checkout_state(sales_invoice)
	existing = _get_active_payrexx_payment_requests(sales_invoice.name, for_update=True)
	if len(existing) > 1:
		frappe.throw(
			_(
				"Multiple active Payrexx Payment Requests exist for this invoice. "
				"The accounts team must review them before online payment can continue."
			)
		)
	if existing:
		raise _CheckoutLockOrderRetry

	conflicting_draft = frappe.db.get_value(
		"Payment Request",
		{
			"reference_doctype": "Sales Invoice",
			"reference_name": sales_invoice.name,
			"docstatus": 0,
		},
		["name", "payment_gateway"],
		as_dict=True,
		for_update=True,
	)
	if conflicting_draft:
		# ERPNext reuses any draft for the reference document, regardless of the
		# requested gateway. Never delete or submit a pre-existing draft.
		frappe.logger("payrexx_integration").warning(
			f"Pay-link creation preserved conflicting draft Payment Request {conflicting_draft.name} "
			f"for Sales Invoice {sales_invoice.name} (gateway {conflicting_draft.payment_gateway or 'unset'})"
		)
		frappe.throw(
			_(
				"A draft Payment Request already exists for this invoice and was preserved. "
				"Please ask the accounts team to review it before retrying online payment."
			)
		)

	from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request

	payment_request = make_payment_request(
		dt="Sales Invoice",
		dn=sales_invoice.name,
		ref_doc=sales_invoice,
		payment_gateway=gateway,
		payment_gateway_account=_gateway_account_filter(sales_invoice, gateway),
		submit_doc=1,
		mute_email=1,
		return_doc=True,
	)
	_validate_payment_request_checkout_state(
		payment_request,
		sales_invoice,
		expected_gateway=gateway,
		require_submitted=True,
	)
	return payment_request, sales_invoice


def _get_payment_request_checkout_url(payment_request, sales_invoice, settings_name: str) -> str:
	"""Return the one checkout created by Payment Request submission."""
	expected_gateway = f"Payrexx-{settings_name}"
	# Existing checkout reuse follows the settlement order exactly: Integration
	# Request, every active Payrexx Payment Request, then Sales Invoice.
	active_requests = _get_active_checkout_requests(payment_request.name)
	if len(active_requests) > 1:
		frappe.throw(
			_(
				"Multiple active Payrexx checkouts exist for Payment Request {0}. "
				"No checkout was reused; please ask the accounts team to review them."
			).format(payment_request.name)
		)
	if active_requests:
		active_payment_requests = _get_active_payrexx_payment_requests(
			sales_invoice.name,
			for_update=True,
		)
		_reject_competing_active_payment_requests(active_payment_requests, payment_request.name)
		payment_request = frappe.get_doc("Payment Request", payment_request.name, for_update=True)
		sales_invoice = frappe.get_doc("Sales Invoice", sales_invoice.name, for_update=True)
		expected_amount, expected_currency = _validate_payment_request_checkout_state(
			payment_request,
			sales_invoice,
			expected_gateway=expected_gateway,
			require_submitted=True,
		)
		checkout_url = _validated_checkout_url(
			active_requests[0],
			payment_request,
			settings_name=settings_name,
			expected_amount=expected_amount,
			expected_currency=expected_currency,
		)
		if payment_request.payment_url and payment_request.payment_url != checkout_url:
			frappe.throw(
				_(
					"The Payment Request URL does not match its active Payrexx checkout. "
					"No checkout was reused; please review Integration Request {0}."
				).format(active_requests[0].name)
			)
	else:
		# A submitted manual request may not have a checkout yet. Serialize on
		# the source, lock all active requests, then recheck for an Integration
		# Request before the one allowed provider POST.
		sales_invoice = frappe.get_doc("Sales Invoice", sales_invoice.name, for_update=True)
		active_payment_requests = _get_active_payrexx_payment_requests(
			sales_invoice.name,
			for_update=True,
		)
		_reject_competing_active_payment_requests(active_payment_requests, payment_request.name)
		payment_request = frappe.get_doc("Payment Request", payment_request.name, for_update=True)
		expected_amount, expected_currency = _validate_payment_request_checkout_state(
			payment_request,
			sales_invoice,
			expected_gateway=expected_gateway,
			require_submitted=True,
		)
		if _get_active_checkout_requests(payment_request.name):
			raise _CheckoutLockOrderRetry
		if payment_request.payment_url:
			frappe.throw(
				_(
					"The Payment Request has a stored URL but no active matching Payrexx checkout. "
					"No checkout was reused; please ask the accounts team to review it."
				)
			)
		# Legacy/manual Payment Requests can be submitted without a checkout.
		# Generate it once while holding both source and Payment Request locks.
		checkout_url = payment_request.get_payment_url()
		if not checkout_url:
			frappe.throw(_("Could not generate Payrexx payment URL"))

		active_requests = _get_active_checkout_requests(payment_request.name)
		if len(active_requests) != 1:
			frappe.throw(
				_("Payrexx did not persist exactly one active checkout for Payment Request {0}.").format(
					payment_request.name
				)
			)
		stored_checkout_url = _validated_checkout_url(
			active_requests[0],
			payment_request,
			settings_name=settings_name,
			expected_amount=expected_amount,
			expected_currency=expected_currency,
		)
		if stored_checkout_url != checkout_url:
			frappe.throw(_("Payrexx returned a checkout URL that does not match the persisted request."))

	if not payment_request.payment_url:
		payment_request.db_set("payment_url", checkout_url, update_modified=False)
	return checkout_url


def _reject_competing_active_payment_requests(
	active_payment_requests: list, payment_request_name: str
) -> None:
	if any(row.name != payment_request_name for row in active_payment_requests):
		frappe.throw(
			_(
				"Another active Payrexx Payment Request exists for this invoice. "
				"No checkout was reused or created; please ask the accounts team to review it."
			)
		)


def _get_active_checkout_requests(payment_request_name: str) -> list:
	return frappe.db.get_values(
		"Integration Request",
		filters={
			"reference_doctype": "Payment Request",
			"reference_docname": payment_request_name,
			"integration_request_service": "Payrexx",
			"status": ["in", ("Queued", "Authorized")],
		},
		fieldname=["name", "status", "reference_doctype", "reference_docname", "data"],
		as_dict=True,
		order_by="creation desc",
		for_update=True,
	)


def _validated_checkout_url(
	integration_request,
	payment_request,
	*,
	settings_name: str,
	expected_amount: int,
	expected_currency: str,
) -> str:
	data = frappe.parse_json(integration_request.data) or {}
	if not isinstance(data, dict):
		frappe.throw(
			_("Integration Request {0} does not contain valid checkout metadata.").format(
				integration_request.name
			)
		)
	try:
		stored_amount = _provider_gateway_amount(data.get("payrexx_gateway_amount"))
		original_amount = _canonical_gateway_amount(data.get("amount"), expected_currency)
	except ValueError:
		stored_amount = original_amount = None

	checkout_url = data.get("payrexx_checkout_url")
	metadata_matches = all(
		(
			integration_request.reference_doctype == "Payment Request",
			integration_request.reference_docname == payment_request.name,
			data.get("reference_doctype") == "Payment Request",
			data.get("reference_docname") == payment_request.name,
			data.get("payment_gateway") == f"Payrexx-{settings_name}",
			data.get("payrexx_settings") == settings_name,
			stored_amount == expected_amount,
			original_amount == expected_amount,
			str(data.get("payrexx_gateway_currency") or "").strip().upper() == expected_currency,
			str(data.get("currency") or "").strip().upper() == expected_currency,
			bool(data.get("payrexx_gateway_id")),
			bool(data.get("payrexx_gateway_hash")),
			bool(checkout_url),
		)
	)
	if not metadata_matches:
		frappe.throw(
			_(
				"The existing Payrexx checkout no longer exactly matches the Payment Request. "
				"No checkout was reused; please review Integration Request {0}."
			).format(integration_request.name)
		)
	return checkout_url


def _gateway_account_filter(sales_invoice, gateway: str) -> dict:
	filters = {
		"payment_gateway": gateway,
		"company": sales_invoice.company,
		"currency": sales_invoice.currency,
	}
	if not frappe.db.exists("Payment Gateway Account", filters):
		frappe.throw(
			_("No Payment Gateway Account configured for {0}, company {1}, and currency {2}").format(
				gateway, sales_invoice.company, sales_invoice.currency
			)
		)
	return filters


def _payment_success_redirect_url(integration_request) -> str:
	data = frappe.parse_json(integration_request.data) or {}
	if data.get("redirect_to"):
		return _safe_return_url(data["redirect_to"])

	params = {
		"doctype": integration_request.reference_doctype or data.get("reference_doctype") or "",
		"docname": integration_request.reference_docname or data.get("reference_docname") or "",
	}
	return get_public_url("/payment-success?" + urlencode(params))


def _payment_failed_redirect_url(integration_request) -> str:
	data = frappe.parse_json(integration_request.data) or {}
	params = {
		"doctype": integration_request.reference_doctype or data.get("reference_doctype") or "",
		"docname": integration_request.reference_docname or data.get("reference_docname") or "",
	}
	return get_public_url("/payment-failed?" + urlencode(params))
