# Copyright (c) 2026, Goodvantage GmbH and contributors
# For license information, please see license.txt

import time
from contextlib import contextmanager
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, cstr, flt
from payments.utils import create_payment_gateway

from payrexx_integration.payrexx_integration.payrexx.payrexx_client import PayrexxClient, get_http_status
from payrexx_integration.payrexx_integration.payrexx.webhook_validator import (
	verify_webhook_signature,
)
from payrexx_integration.url_utils import get_public_url, safe_return_url

PAYMENT_AUTHORIZED_MAX_ATTEMPTS = 3


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
		if currency not in self._supported_currencies():
			frappe.throw(
				_("Currency {0} is not supported by Payrexx gateway {1}.").format(currency, self.gateway_name)
			)

	def get_payment_url(self, **kwargs):
		"""
		Create a Payrexx Gateway and return the hosted checkout URL.

		kwargs originate from Payment Request / Subscription / Web Form and include:
		  amount, currency, reference_doctype, reference_docname,
		  payer_name, payer_email, description, redirect_to, title
		"""
		try:
			integration_request = create_request_log(kwargs, service_name="Payrexx")

			payload = self._build_create_gateway_payload(kwargs, integration_request.name)
			gateway = self._client().create_gateway(payload)

			data = frappe.parse_json(integration_request.data) or {}
			data["payrexx_gateway_id"] = gateway.get("id")
			data["payrexx_gateway_hash"] = gateway.get("hash")
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

	def _build_create_gateway_payload(self, kwargs: dict, integration_request_name: str) -> dict:
		# Payrexx wants the amount in the smallest currency unit (e.g. cents).
		amount_cents = round(flt(kwargs.get("amount")) * 100)
		currency = (kwargs.get("currency") or "CHF").upper()
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
		ir_data["payrexx_transaction"] = txn
		ir.data = frappe.as_json(ir_data)

		if status == "confirmed":
			_complete_integration_request(ir, txn)
		elif status in ("authorized", "reserved"):
			ir.status = "Authorized"
			ir.save(ignore_permissions=True)
		elif status in ("cancelled", "declined", "error", "expired", "chargeback"):
			ir.status = "Failed"
			ir.error = f"Payrexx status: {status}"
			ir.save(ignore_permissions=True)
		else:
			# 'waiting' and anything we don't recognise — keep listening.
			ir.save(ignore_permissions=True)

		return {"ok": True}
	except frappe.AuthenticationError:
		raise
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Payrexx callback error")
		raise


def _resolve_settings(gateway_name: str | None) -> "PayrexxSettings":
	if gateway_name:
		return frappe.get_cached_doc("Payrexx Settings", gateway_name)

	rows = frappe.get_all("Payrexx Settings", pluck="name", limit=2)
	if len(rows) == 1:
		return frappe.get_cached_doc("Payrexx Settings", rows[0])
	frappe.throw(_("Multiple Payrexx Settings exist — webhook URL must include ?gateway_name=..."))


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
	if ir.status == "Completed":
		return True

	ir_data = frappe.parse_json(ir.data) or {}
	gateway_id = ir_data.get("payrexx_gateway_id")
	if not gateway_id:
		return False

	settings = _resolve_settings(gateway_name or _settings_name_from_request_data(ir_data))
	gateway = settings._client().retrieve_gateway(int(gateway_id))
	status = (gateway.get("status") or "").lower()
	transaction = _confirmed_transaction_from_gateway(gateway)

	if status == "confirmed" or (transaction and (transaction.get("status") or "").lower() == "confirmed"):
		_complete_integration_request(ir, transaction or gateway)
		return True
	if status in ("cancelled", "declined", "error", "expired", "chargeback"):
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
				return transaction
	return {}


def _complete_integration_request(integration_request, transaction: dict | None = None) -> None:
	was_completed = integration_request.status == "Completed"
	ir_data = frappe.parse_json(integration_request.data) or {}
	if transaction:
		ir_data["payrexx_transaction"] = transaction
	integration_request.data = frappe.as_json(ir_data)
	integration_request.status = "Completed"
	integration_request.save(ignore_permissions=True)
	if not was_completed:
		_run_payment_authorized_with_retries(integration_request.name, "Completed")


def _run_payment_authorized_with_retries(integration_request_name: str, status: str) -> None:
	for attempt in range(1, PAYMENT_AUTHORIZED_MAX_ATTEMPTS + 1):
		try:
			integration_request = frappe.get_doc("Integration Request", integration_request_name)
			_on_payment_authorized(integration_request, status)
			return
		except frappe.QueryDeadlockError:
			frappe.db.rollback()
			if attempt == PAYMENT_AUTHORIZED_MAX_ATTEMPTS:
				raise
			time.sleep(0.25 * attempt)


def _on_payment_authorized(integration_request, status):
	if not (integration_request.reference_doctype and integration_request.reference_docname):
		return
	try:
		with _payment_authorization_user():
			frappe.get_doc(
				integration_request.reference_doctype,
				integration_request.reference_docname,
			).run_method("on_payment_authorized", status)
	except frappe.QueryDeadlockError:
		raise
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Payrexx on_payment_authorized")
		raise


# Duplicates good_connector.workflow_support.as_automation_user — kept locally
# because payrexx_integration does not depend on good_connector.
@contextmanager
def _payment_authorization_user():
	automation_user = _payment_authorization_user_name()
	previous_user = frappe.session.user
	previous_sid = getattr(frappe.session, "sid", None)
	previous_data = getattr(frappe.session, "data", None)

	if automation_user and automation_user != previous_user:
		frappe.set_user(automation_user)  # nosemgrep: frappe-setuser

	try:
		yield
	finally:
		if automation_user and automation_user != previous_user:
			frappe.set_user(previous_user)  # nosemgrep: frappe-setuser
			frappe.session.sid = previous_sid
			frappe.session.data = previous_data


def _payment_authorization_user_name() -> str:
	if frappe.db.exists("DocType", "Non Profit Settings"):
		creation_user = frappe.db.get_single_value("Non Profit Settings", "creation_user")
		if creation_user and frappe.db.exists("User", creation_user):
			return creation_user

	return "Administrator"
