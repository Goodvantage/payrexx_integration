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
from urllib.parse import urlencode

import frappe
from frappe import _

from payrexx_integration.gateway_selection import resolve_payrexx_settings
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
		frappe.log_error(frappe.get_traceback(), "Payrexx pay URL unavailable")
		return ""
	params = {
		"si": sales_invoice,
		"gateway_name": settings_name,
		"token": _sign(sales_invoice, settings_name),
	}
	return get_public_url("/api/method/payrexx_integration.api.pay_invoice?" + urlencode(params))


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
	with as_automation_user():
		payment_request = _get_or_create_payment_request(sales_invoice, settings_name)
		checkout_url = _get_payment_request_checkout_url(payment_request)

	# Email links must remain GET requests, but successful first-click setup writes
	# the Payment Request and Integration Request. Frappe rolls back GET requests
	# unless this framework flag is set; set it only after the checkout is valid.
	frappe.local.flags.commit = True
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = checkout_url


@frappe.whitelist(allow_guest=True, methods=["GET"])  # nosemgrep: guest-whitelisted-method
def payment_success(ir: str | None = None, gateway_name: str | None = None) -> None:
	"""Reconcile a Payrexx success redirect, then send the customer to the final return URL."""
	if not ir or not frappe.db.exists("Integration Request", ir):
		frappe.throw(_("Payment reference not found"), frappe.DoesNotExistError)

	from payrexx_integration.payrexx_integration.doctype.payrexx_settings import (
		payrexx_settings,
	)

	reconciled = payrexx_settings.reconcile_integration_request(ir, gateway_name=gateway_name)
	integration_request = frappe.get_doc("Integration Request", ir)
	if not reconciled and integration_request.status != "Completed":
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = _payment_failed_redirect_url(integration_request)
		return
	redirect_url = _payment_success_redirect_url(integration_request)
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = redirect_url


# ------------------------------------------------------------------- internals


def _get_or_create_payment_request(sales_invoice, settings_name: str):
	# Serialize first-click creation so concurrent email-link requests cannot
	# create separate Payment Requests and Payrexx Gateways for one invoice.
	frappe.db.get_value("Sales Invoice", sales_invoice.name, "name", for_update=True)
	sales_invoice.reload()

	gateway = "Payrexx-" + settings_name
	existing = frappe.get_all(
		"Payment Request",
		filters={
			"reference_doctype": "Sales Invoice",
			"reference_name": sales_invoice.name,
			"payment_gateway": gateway,
			"status": ["in", ("Draft", "Requested", "Initiated", "Partially Paid")],
			"docstatus": ["<", 2],
		},
		pluck="name",
		limit=1,
		order_by="creation desc",
	)
	if existing:
		return frappe.get_doc("Payment Request", existing[0])

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
		payment_gateway=gateway,
		payment_gateway_account=_gateway_account_filter(sales_invoice, gateway),
		submit_doc=1,
		mute_email=1,
		return_doc=True,
	)
	return payment_request


def _get_payment_request_checkout_url(payment_request) -> str:
	"""Return the one checkout created by Payment Request submission."""
	frappe.db.get_value("Payment Request", payment_request.name, "name", for_update=True)
	payment_request.reload()
	if payment_request.payment_url:
		return payment_request.payment_url

	active_requests = frappe.get_all(
		"Integration Request",
		filters={
			"reference_doctype": "Payment Request",
			"reference_docname": payment_request.name,
			"integration_request_service": "Payrexx",
			"status": ["in", ("Queued", "Authorized")],
		},
		fields=["name", "data"],
		order_by="creation desc",
		limit=1,
	)
	if active_requests:
		checkout_url = (frappe.parse_json(active_requests[0].data) or {}).get("payrexx_checkout_url")
		if not checkout_url:
			frappe.throw(
				_(
					"The existing Payrexx checkout has no stored URL. No duplicate checkout was created; "
					"please review Integration Request {0}."
				).format(active_requests[0].name)
			)
	else:
		# Legacy/manual Payment Requests can be submitted without a checkout.
		# Generate it once while holding the Payment Request lock.
		checkout_url = payment_request.get_payment_url()

	if not checkout_url:
		frappe.throw(_("Could not generate Payrexx payment URL"))
	payment_request.db_set("payment_url", checkout_url, update_modified=False)
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
