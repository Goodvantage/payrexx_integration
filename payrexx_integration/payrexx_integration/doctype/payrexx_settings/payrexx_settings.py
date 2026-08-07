# Copyright (c) 2026, Goodvantage GmbH and contributors
# For license information, please see license.txt

import hashlib
import json
import logging
import re
import time
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlsplit

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import call_hook_method, cint, cstr, flt, get_datetime, now_datetime
from payments.utils import create_payment_gateway

from payrexx_integration.gateway_selection import resolve_payrexx_settings
from payrexx_integration.payrexx_integration.payrexx import webhook_payload
from payrexx_integration.payrexx_integration.payrexx.payrexx_client import (
	PayrexxClient,
	_normalize_api_base_domain,
	get_http_status,
	validate_subscription_interval,
)
from payrexx_integration.payrexx_integration.payrexx.webhook_validator import (
	verify_webhook_signature,
)
from payrexx_integration.session_utils import as_automation_user, payment_authorization_user_name
from payrexx_integration.url_utils import get_public_url, is_allowed_public_origin, safe_return_url

DEADLOCK_MAX_ATTEMPTS = 3
ACTIVE_PAYREXX_PAYMENT_REQUEST_STATUSES = (
	"Requested",
	"Initiated",
	"Partially Paid",
	"Payment Ordered",
)
CHECKOUT_PROVIDER_CONTACT_FLAG = "payrexx_checkout_provider_contacted"
CHARGEBACK_TODO_MARKER = "[Payrexx chargeback]"
CHARGEBACK_ERROR = "Payrexx status: chargeback"
REFUND_TODO_MARKER = "[Payrexx refund]"
DISPUTE_TODO_MARKER = "[Payrexx dispute]"
REVERSAL_DATA_KEY = "payrexx_reversals"
REFUND_NOTICE_PROVIDER_HOOK = "payrexx_refund_notice_providers"
REFUND_STATUSES = ("refunded", "partially-refunded", "refund_pending")
# Statuses that describe what happened *after* a payment settled. They must
# reach a Completed request — that is the only state a refund can follow — while
# every other delayed or replayed status stays ignored.
POST_SETTLEMENT_STATUSES = frozenset({"chargeback", "disputed", *REFUND_STATUSES})
SETTLEMENT_CONFLICT_TODO_MARKER = "[Payrexx settlement conflict]"
SETTLEMENT_CONFLICT_DATA_KEY = "payrexx_settlement_conflict"
SETTLEMENT_CONFLICT_VERSION = 1
SETTLEMENT_SOURCE_PROVIDER_HOOK = "payrexx_settlement_source_providers"
SUBSCRIPTION_EVENT_PROVIDER_HOOK = "payrexx_subscription_event_providers"
SUBSCRIPTION_EVENT_DOCTYPE = "Payrexx Subscription Event"
# One provider transaction can be delivered repeatedly as its status advances.
# Confirmed is the highest financial state; failures may still advance to it if
# the provider later succeeds, while delayed preliminary states never downgrade
# a terminal observation.
SUBSCRIPTION_INSTALLMENT_STATUS_STAGE = {
	"waiting": 10,
	"authorized": 20,
	"reserved": 20,
	"uncaptured": 30,
	"cancelled": 30,
	"declined": 30,
	"error": 30,
	"expired": 30,
	"confirmed": 40,
}
SUBSCRIPTION_INSTALLMENT_STATUSES = frozenset(SUBSCRIPTION_INSTALLMENT_STATUS_STAGE)
SUBSCRIPTION_REVERSAL_STATUS_STAGE = {
	"refund_pending": 10,
	"refunded": 20,
	"partially-refunded": 20,
	"disputed": 20,
	"chargeback": 20,
}
TRANSACTION_RECONCILIATION_INITIAL_LOOKBACK = timedelta(days=7)
TRANSACTION_RECONCILIATION_OVERLAP = timedelta(hours=6)
TRANSACTION_RECONCILIATION_MAX_WINDOW = timedelta(days=7)
TRANSACTION_RECONCILIATION_PAGE_SIZE = 100
TRANSACTION_RECONCILIATION_MAX_PAGES = 100
GATEWAY_RECOVERY_LOG_MARKER = "[Payrexx Gateway recovery]"
GATEWAY_ORPHAN_LOG_MARKER = "[Payrexx possible orphan Gateway]"
PAYREXX_SUCCESS_TOKEN_VERSION_KEY = "payrexx_success_token_version"
PAYREXX_SUCCESS_TOKEN_VERSION = 1
DECIMAL_CONVERSION_ERRORS = (InvalidOperation, TypeError, ValueError)
_QR_SESSION_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_QR_CODE_UUID_PATTERN = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_MAX_WEBSHOP_URL_LENGTH = 1000
# A QR code already removed on the provider side is a normal deletion outcome,
# not an error: tolerated by this controller and declared to the client so it
# writes no Error Log row either.
QR_DELETE_TOLERATED_STATUSES = (404,)


class SubscriptionEventDispatchError(RuntimeError):
	"""An installment provider failed after the webhook was authenticated."""


def _get_current_locked_doc(doctype: str, name: str) -> Document:
	"""Hydrate and lock a document in one current read, outside any stale transaction snapshot."""
	return frappe.get_doc(doctype, name, for_update=True)


def _run_with_deadlock_retry(operation):
	"""Rollback a failed transaction before replaying its complete atomic operation."""
	for attempt in range(1, DEADLOCK_MAX_ATTEMPTS + 1):
		try:
			return operation()
		except frappe.QueryDeadlockError:
			frappe.db.rollback()
			if attempt == DEADLOCK_MAX_ATTEMPTS:
				raise
			time.sleep(0.25 * attempt)


class PayrexxSettings(Document):
	def validate(self):
		payment_authorization_user_name(self)
		try:
			self.api_base_domain = _normalize_api_base_domain(self.get("api_base_domain"))
		except ValueError as exc:
			frappe.throw(
				_("Invalid Payrexx API Base Domain: {0}").format(cstr(exc)),
				frappe.ValidationError,
			)
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
		with as_automation_user(self):
			self._validate_payment_request_source(kwargs)
			# _client validates the destination before get_password reads the API key.
			client = self._client()
			integration_request = None
			provider_contacted = False
			gateway = None
			try:
				integration_request = _create_integration_request(kwargs, self.name)

				payload = self._build_create_gateway_payload(kwargs, integration_request.name)
				provider_contacted = True
				frappe.flags[CHECKOUT_PROVIDER_CONTACT_FLAG] = True
				gateway = client.create_gateway(payload)
				_register_gateway_orphan_recovery(
					integration_request,
					gateway,
					settings_name=self.name,
				)
				gateway = _validate_created_gateway(gateway)

				data = frappe.parse_json(integration_request.data) or {}
				data["payrexx_gateway_id"] = gateway.get("id")
				data["payrexx_gateway_hash"] = gateway.get("hash")
				data["payrexx_checkout_url"] = gateway.get("link")
				data["payrexx_gateway_amount"] = payload["amount"]
				data["payrexx_gateway_currency"] = payload["currency"]
				if gateway.get("appLink"):
					data["payrexx_gateway_app_link"] = gateway["appLink"]
				# Authoritative record of which settings row created this request —
				# the webhook only accepts callbacks verified with this row's key.
				data["payrexx_settings"] = self.name
				integration_request.data = frappe.as_json(data)
				integration_request.save(ignore_permissions=True)

				# A TWINT static-QR scan must hand the donor back into the TWINT app;
				# every other checkout redirects to the hosted payment page.
				if payload.get("qrCodeSessionId") and gateway.get("appLink"):
					return gateway["appLink"]
				return gateway["link"]
			except frappe.QueryDeadlockError:
				if provider_contacted and gateway is None and integration_request:
					_log_unknown_gateway_outcome(integration_request.name, self.name)
				raise
			except Exception:
				if provider_contacted and gateway is None and integration_request:
					_log_unknown_gateway_outcome(integration_request.name, self.name)
				frappe.log_error(title="Payrexx get_payment_url", message=frappe.get_traceback())
				frappe.throw(_("Could not generate Payrexx payment URL"))

	# ---------------------------------------------------------- static QR codes

	def create_static_qr(self, webshop_url: str) -> dict:
		"""Create a permanent Payrexx static QR code pointing at ``webshop_url``.

		Returns the provider payload: ``uuid``, ``webshopUrl``, ``png``, ``svg``
		(the images are base64 data URIs). A plain camera scan opens the URL
		unchanged; a TWINT-app scan opens it with ``qr_code_session_id`` plus
		``returnAppScheme`` (iOS) or ``returnAppPackage`` (Android) appended.
		The landing page must forward those into checkout creation via the
		``qr_code_session_id`` / ``return_app`` kwargs of ``get_payment_url``.

		Runs as the owning row's automation user like every other
		settings-controller provider operation. Not whitelisted — callers own
		permission checks.
		"""
		webshop_url = _validate_webshop_url(webshop_url)
		with as_automation_user(self):
			client = self._client()
			try:
				qr_code = client.create_qr_code(webshop_url)
			except Exception:
				frappe.log_error(title="Payrexx create_static_qr", message=frappe.get_traceback())
				frappe.throw(_("Could not create Payrexx QR code"))
			if not isinstance(qr_code, dict) or not qr_code.get("uuid"):
				frappe.throw(_("Payrexx returned incomplete QR code metadata"))
			return qr_code

	def delete_static_qr(self, qr_code_uuid: str) -> None:
		"""Delete a static QR code on Payrexx.

		A provider-side 404 counts as deleted so a code removed in the Payrexx
		dashboard cannot wedge local cleanup. That tolerated outcome is declared to
		the client as well, so a normal "already gone" deletion writes no Error Log
		row for staff to triage. Runs as the owning row's automation user. Not
		whitelisted — callers own permission checks.
		"""
		qr_code_uuid = _validate_qr_code_uuid(qr_code_uuid)
		with as_automation_user(self):
			client = self._client()
			try:
				client.delete_qr_code(qr_code_uuid, expected_statuses=QR_DELETE_TOLERATED_STATUSES)
			except Exception as exc:
				if get_http_status(exc) in QR_DELETE_TOLERATED_STATUSES:
					return
				frappe.log_error(title="Payrexx delete_static_qr", message=frappe.get_traceback())
				frappe.throw(_("Could not delete Payrexx QR code"))

	# ------------------------------------------------------------------ helpers

	def _client(self) -> PayrexxClient:
		try:
			api_base_domain = _normalize_api_base_domain(self.get("api_base_domain"))
		except ValueError as exc:
			frappe.throw(
				_("Invalid Payrexx API Base Domain: {0}").format(cstr(exc)),
				frappe.ValidationError,
			)
		return PayrexxClient(
			instance=self.instance_name,
			# Never move this Password-field read before destination validation.
			api_secret=self.get_password("api_secret"),
			api_base_domain=api_base_domain,
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
			if _extension_source_validation(phase="checkout", settings=self, kwargs=kwargs) is True:
				return
			frappe.throw(_("Payrexx supports only Payment Requests for Sales Invoices."))

		payment_request_source = frappe.db.get_value(
			"Payment Request",
			kwargs.get("reference_docname"),
			["reference_doctype", "reference_name"],
			as_dict=True,
		)
		if not payment_request_source:
			frappe.throw(_("Referenced Payment Request was not found."))
		if payment_request_source.reference_doctype != "Sales Invoice":
			frappe.throw(_("Payrexx supports only Payment Requests for Sales Invoices."))
		if not payment_request_source.reference_name:
			frappe.throw(_("The Payment Request does not identify a Sales Invoice."))

		# A new checkout has no externally visible Integration Request yet, so the
		# Sales Invoice is its serialization boundary. Lock every active Payrexx
		# request before hydrating this request and before contacting the provider.
		sales_invoice = _get_current_locked_doc("Sales Invoice", payment_request_source.reference_name)
		active_payment_requests = _get_active_payrexx_payment_requests(
			sales_invoice.name,
			for_update=True,
		)
		if any(row.name != kwargs.get("reference_docname") for row in active_payment_requests):
			frappe.throw(
				_(
					"Another active Payrexx Payment Request already exists for this Sales Invoice. "
					"It was preserved; the accounts team must review it before another checkout can be created."
				)
			)
		payment_request = _get_current_locked_doc("Payment Request", kwargs.get("reference_docname"))
		_validate_payment_request_checkout_state(
			payment_request,
			sales_invoice,
			expected_gateway=f"Payrexx-{self.name}",
			requested_amount=kwargs.get("amount"),
			requested_currency=kwargs.get("currency"),
			require_submitted=False,
		)

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

		if kwargs.get("subscription_state"):
			if not cint(self.get("enable_managed_subscriptions")):
				frappe.throw(
					_(
						"Managed subscriptions are disabled for Payrexx gateway {0}. "
						"Enable them only after signed sandbox verification."
					).format(self.name),
					frappe.ValidationError,
				)
			payload.update(_subscription_gateway_payload(kwargs))

		# Static-QR TWINT handoff: both values arrive as guest-controlled query
		# parameters appended by the TWINT app, so invalid values are dropped
		# silently — the checkout still works as a plain hosted page.
		qr_code_session_id = _sanitize_qr_session_value(kwargs.get("qr_code_session_id"))
		if qr_code_session_id:
			payload["qrCodeSessionId"] = qr_code_session_id
			return_app = _sanitize_qr_session_value(kwargs.get("return_app"))
			if return_app:
				payload["returnApp"] = return_app

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
			from payrexx_integration.api import sign_payment_success_reference

			return get_public_url(
				"/api/method/payrexx_integration.api.payment_success?"
				+ urlencode(
					{
						"ir": integration_request_name,
						"gateway_name": self.name,
						"token": sign_payment_success_reference(integration_request_name, self.name),
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


def _create_integration_request(data: dict, settings_name: str):
	"""Insert a Payrexx request log without committing the caller's transaction."""
	request_data = {
		**data,
		"payrexx_settings": settings_name,
		PAYREXX_SUCCESS_TOKEN_VERSION_KEY: PAYREXX_SUCCESS_TOKEN_VERSION,
	}
	integration_request = frappe.get_doc(
		{
			"doctype": "Integration Request",
			"integration_request_service": "Payrexx",
			"status": "Queued",
			"data": frappe.as_json(request_data),
			"reference_doctype": data.get("reference_doctype"),
			"reference_docname": data.get("reference_docname"),
		}
	)
	integration_request.insert(ignore_permissions=True)
	return integration_request


def _get_active_payrexx_payment_requests(
	sales_invoice_name: str,
	*,
	for_update: bool,
) -> list:
	"""Read active Payrexx requests for an invoice, optionally locking current rows."""
	return frappe.db.get_values(
		"Payment Request",
		filters={
			"reference_doctype": "Sales Invoice",
			"reference_name": sales_invoice_name,
			"payment_gateway": ["like", "Payrexx-%"],
			"status": ["in", ACTIVE_PAYREXX_PAYMENT_REQUEST_STATUSES],
			"docstatus": 1,
		},
		fieldname=["name", "payment_gateway", "status"],
		as_dict=True,
		order_by="name asc",
		for_update=for_update,
	)


def _validate_created_gateway(gateway: dict) -> dict:
	if not isinstance(gateway, dict) or any(not gateway.get(field) for field in ("id", "hash", "link")):
		raise ValueError("Payrexx returned incomplete Gateway metadata")
	return gateway


def _subscription_gateway_payload(kwargs: dict) -> dict:
	"""Turn a checkout into a subscription signup.

	Unlike the QR handoff below, an invalid value here is never dropped
	silently: a subscription created on the wrong interval bills the payer
	wrongly for as long as it runs, so a bad interval must fail the checkout
	rather than quietly become a one-off payment or a different cadence.

	``subscriptionPeriod`` and ``subscriptionCancellationInterval`` are sent
	only when supplied. There is no default: the values Payrexx accepts for an
	open-ended subscription are the least documented part of this contract, and
	guessing one here would be indistinguishable from a deliberate choice.
	"""
	try:
		payload = {
			"subscriptionState": True,
			"subscriptionInterval": validate_subscription_interval(
				kwargs.get("subscription_interval"), "subscription_interval"
			),
		}
		for key, provider_key in (
			("subscription_period", "subscriptionPeriod"),
			("subscription_cancellation_interval", "subscriptionCancellationInterval"),
		):
			if kwargs.get(key):
				payload[provider_key] = validate_subscription_interval(kwargs.get(key), key)
	except ValueError as exc:
		frappe.throw(cstr(exc), frappe.ValidationError)
	return payload


def _sanitize_qr_session_value(value) -> str | None:
	"""Return a provider-safe QR session value, or None to drop it.

	``qr_code_session_id`` and the return-app value originate from the query
	string of a guest request (the TWINT app appends them to the scanned URL).
	They must never fail a checkout — without them the payment simply proceeds
	as a plain hosted checkout.
	"""
	value = cstr(value).strip()
	if not value or not _QR_SESSION_VALUE_PATTERN.fullmatch(value):
		return None
	return value


def _validate_webshop_url(value) -> str:
	url = cstr(value).strip()
	if len(url) > _MAX_WEBSHOP_URL_LENGTH:
		frappe.throw(_("Invalid QR code target URL"))
	try:
		parts = urlsplit(url)
	except ValueError:
		frappe.throw(_("Invalid QR code target URL"))
	if (
		parts.scheme not in ("http", "https")
		or not parts.netloc
		or parts.username is not None
		or parts.password is not None
	):
		frappe.throw(_("Invalid QR code target URL"))
	# A static QR is permanent and printed, so a stale or mistyped public base in
	# the calling app would mint codes pointing at a foreign origin forever. Bind
	# the target to an origin this site actually publishes — ``host_name`` or an
	# operator-configured ``*_public_base_url`` — which is the same allowlist
	# ``safe_return_url`` enforces and exactly what upstream apps build their
	# public URLs from.
	if not is_allowed_public_origin(url):
		frappe.throw(_("Invalid QR code target URL"))
	return url


def _validate_qr_code_uuid(value) -> str:
	qr_code_uuid = cstr(value).strip()
	if not _QR_CODE_UUID_PATTERN.fullmatch(qr_code_uuid):
		frappe.throw(_("Invalid Payrexx QR code UUID"))
	return qr_code_uuid


def _register_gateway_orphan_recovery(integration_request, gateway: dict, *, settings_name: str) -> None:
	"""Journal provider creation before local commit and record its eventual outcome."""
	evidence = {
		"gateway_id": gateway.get("id") if isinstance(gateway, dict) else None,
		"integration_request": integration_request.name,
		"reference_doctype": integration_request.reference_doctype,
		"reference_docname": integration_request.reference_docname,
		"settings": settings_name,
	}
	frappe.db.after_commit.add(lambda: _log_gateway_recovery_committed(evidence))
	frappe.db.after_rollback.add(lambda: _log_gateway_orphan_recovery(evidence))
	_log_gateway_recovery_pending(evidence)


def _gateway_recovery_logger():
	logger = frappe.logger("payrexx_integration")
	# Production Frappe loggers default to ERROR. Recovery journal INFO/WARNING
	# records must still reach the rotating file to distinguish commit outcomes.
	if not logger.isEnabledFor(logging.INFO):
		logger.setLevel(logging.INFO)
	return logger


def _log_gateway_recovery_pending(evidence: dict) -> None:
	_gateway_recovery_logger().warning(
		f"{GATEWAY_RECOVERY_LOG_MARKER} state=local_commit_pending. Payrexx returned a Gateway; "
		f"local commit is not yet proven: {frappe.as_json(evidence)}"
	)


def _log_gateway_recovery_committed(evidence: dict) -> None:
	_gateway_recovery_logger().info(
		f"{GATEWAY_RECOVERY_LOG_MARKER} state=local_commit_confirmed: {frappe.as_json(evidence)}"
	)


def _log_gateway_orphan_recovery(evidence: dict) -> None:
	_gateway_recovery_logger().critical(
		f"{GATEWAY_ORPHAN_LOG_MARKER} state=local_rollback_confirmed. "
		"Local checkout state rolled back after a provider response. "
		f"Search Payrexx by referenceId and verify/delete an unused Gateway: {frappe.as_json(evidence)}"
	)


def _log_unknown_gateway_outcome(integration_request_name: str, settings_name: str) -> None:
	_gateway_recovery_logger().warning(
		f"{GATEWAY_ORPHAN_LOG_MARKER} Gateway creation outcome is unknown. Search Payrexx by "
		f"referenceId={integration_request_name} before retrying (settings={settings_name})."
	)


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
def callback(gateway_name: str | None = None) -> dict[str, bool | str]:
	"""
	Configure the following URL in Payrexx -> Webhooks for each gateway:

	  https://<your-site>/api/method/payrexx_integration.payrexx_integration.\
doctype.payrexx_settings.payrexx_settings.callback?gateway_name=Live

	The gateway_name query param is required when more than one Payrexx Settings
	row exists, so we know which signing key to verify against.
	"""
	settings = None
	txn = {}
	ref_id = ""
	status = ""
	try:
		raw_body = frappe.request.get_data() or b""
		signature = frappe.get_request_header("X-Webhook-Signature", "")

		settings = _resolve_settings(_gateway_name_from_request(gateway_name))
		if not verify_webhook_signature(raw_body, signature, settings.get_password("webhook_signing_key")):
			frappe.throw(_("Invalid Payrexx webhook signature"), frappe.AuthenticationError)

		# Checked only after the body is authenticated, so an unverified request
		# learns nothing about how we parse it.
		_reject_non_json_webhook_body()

		body = frappe.parse_json(raw_body.decode("utf-8") if raw_body else "{}") or {}

		# A subscription lifecycle delivery carries no transaction and settles no
		# money; it reports what happened to the instruction.
		if webhook_payload.is_subscription_event(body):
			return _process_callback_subscription(settings.name, webhook_payload.subscription_of(body))

		txn = webhook_payload.transaction_of(body)
		if not txn:
			frappe.throw(
				_(
					"Payrexx delivered an unsupported JSON webhook shape. Expected a transaction "
					"envelope or a subscription lifecycle object."
				),
				frappe.ValidationError,
			)
		ref_id = webhook_payload.reference_id(txn)
		status = webhook_payload.transaction_status(txn)

		if not ref_id:
			# A recurring transaction can still be correlated by its provider
			# subscription id. Do not acknowledge a financial event merely because
			# its optional reference is absent.
			if webhook_payload.embedded_subscription(txn):
				return _run_with_deadlock_retry(
					lambda: _process_callback_transaction(settings.name, txn, "", status)
				)
			frappe.log_error(
				title="Payrexx webhook missing referenceId",
				message=frappe.as_json(_webhook_log_summary(txn, ref_id, status)),
			)
			return {"ok": True}

		return _run_with_deadlock_retry(
			lambda: _process_callback_transaction(settings.name, txn, ref_id, status)
		)
	except SubscriptionEventDispatchError:
		# The provider may have written documents or registered callbacks before it
		# failed. Roll back the complete webhook transaction first, then retain only
		# a sanitized, locally replayable Unclaimed event in the fresh transaction.
		frappe.db.rollback()
		with as_automation_user(settings):
			_persist_unclaimed_subscription_event(settings.name, txn, ref_id, status)
		frappe.local.response["http_status_code"] = 503
		frappe.log_error(
			title="Payrexx subscription charge provider failed",
			message=frappe.as_json(
				_subscription_log_summary(webhook_payload.embedded_subscription(txn))
				| {"reference": ref_id or None}
			),
		)
		return {"ok": False, "error": "subscription_event_unclaimed"}
	except frappe.AuthenticationError:
		raise
	except Exception:
		frappe.log_error(title="Payrexx callback error", message=frappe.get_traceback())
		raise


def _reject_non_json_webhook_body() -> None:
	"""Fail a form-encoded delivery with the setting that produced it.

	A Payrexx webhook can be configured to deliver "Normal (PHP-Post)" form
	encoding instead of JSON. That body is not JSON and never will be, so it is
	worth naming the merchant-account setting rather than letting it surface as
	a parse error against an authentic, correctly-signed request.
	"""
	# Read from the request object the raw body came from, not the header
	# helper, so body and content type can never describe different requests.
	content_type = cstr(getattr(getattr(frappe, "request", None), "content_type", "")).lower()
	if content_type and "json" not in content_type:
		frappe.throw(
			_(
				"Payrexx delivered this webhook as {0}. Set the webhook's content type to JSON "
				"in the Payrexx merchant account (Webhooks -> edit -> content type)."
			).format(content_type.split(";")[0]),
			frappe.ValidationError,
		)


def _process_callback_subscription(settings_name: str, subscription: dict) -> dict[str, bool]:
	"""Report a subscription lifecycle change to whoever owns the instruction.

	This app has no concept of a recurring donation or a membership, so it does
	not interpret the status — it hands the event to the owning app and records
	nothing of its own. No money moves on these deliveries.
	"""
	if (
		not webhook_payload.subscription_id(subscription)
		or webhook_payload.subscription_status(subscription) not in webhook_payload.SUBSCRIPTION_STATUSES
	):
		frappe.throw(
			_("The Payrexx subscription lifecycle payload is missing a supported id or status."),
			frappe.ValidationError,
		)
	with as_automation_user(settings_name):
		if not _dispatch_subscription_event("status", subscription=subscription, settings_name=settings_name):
			frappe.log_error(
				title="Payrexx subscription event unclaimed",
				message=frappe.as_json(_subscription_log_summary(subscription)),
			)
	_enqueue_subscription_transaction_reconciliation(settings_name, subscription)
	return {"ok": True}


def _process_callback_transaction(
	settings_name: str,
	transaction: dict,
	reference_id: str,
	status: str,
) -> dict[str, bool | str]:
	"""Apply one authenticated callback attempt; deadlocks propagate to the public boundary."""
	subscription = webhook_payload.embedded_subscription(transaction)
	if reference_id:
		try:
			# Classification of a recurring charge must use the same current row read
			# that owns the callback mutation. A preliminary scalar read can retain a
			# stale REPEATABLE READ snapshot and discard an installment that raced the
			# signup settlement.
			ir = _get_current_locked_doc("Integration Request", reference_id)
		except frappe.DoesNotExistError:
			ir = None
	else:
		ir = None
	if not ir:
		if subscription:
			if status in POST_SETTLEMENT_STATUSES:
				return _process_subscription_reversal(
					settings_name,
					transaction,
					subscription,
					reference_id,
					status,
				)
			return _process_subscription_charge(
				settings_name,
				transaction,
				subscription,
				reference_id,
				status,
				integration_request=None,
			)
		frappe.log_error(
			title="Payrexx webhook unknown reference",
			message=frappe.as_json(_webhook_log_summary(transaction, reference_id, status)),
		)
		return {"ok": True}

	if ir.integration_request_service != "Payrexx":
		frappe.log_error(
			title="Payrexx webhook wrong Integration Request service",
			message=frappe.as_json(_webhook_log_summary(transaction, reference_id, status)),
		)
		return {"ok": True}

	ir_data = frappe.parse_json(ir.data) or {}

	# Bind the verifying key to the Integration Request's own gateway: a
	# webhook signed with one row's key (e.g. Sandbox) must not complete a
	# request created by another row (e.g. Live).
	expected_settings = ir_data.get("payrexx_settings") or _settings_name_from_request_data(ir_data)
	if expected_settings and expected_settings != settings_name:
		frappe.log_error(
			title="Payrexx webhook gateway mismatch",
			message=frappe.as_json(
				_webhook_log_summary(transaction, reference_id, status)
				| {"verified_with": settings_name, "expected": expected_settings}
			),
		)
		return {"ok": True}
	if (
		subscription
		and status in POST_SETTLEMENT_STATUSES
		and not _subscription_reversal_targets_signup(ir_data, transaction, status)
	):
		return _process_subscription_reversal(
			settings_name,
			transaction,
			subscription,
			reference_id,
			status,
		)

	# Refunds, disputes, and chargebacks belong to the established reversal
	# state machine. Sending them through the installment hook first would hide
	# accounting evidence behind a durable charge claim.
	if subscription and status not in POST_SETTLEMENT_STATUSES:
		handled = _process_subscription_charge(
			settings_name,
			transaction,
			subscription,
			reference_id,
			status,
			integration_request=ir,
		)
		if handled is not None:
			return handled
	# Chargeback evidence is terminal. Only an authentic duplicate chargeback
	# may re-enter its idempotent ToDo repair path.
	if _is_chargeback_recorded(ir, ir_data):
		if status == "chargeback":
			_mark_locked_chargeback(ir.name, transaction, settings_name=settings_name)
		return {"ok": True}

	# A confirmed settlement conflict is an accounting terminal state. Keep
	# authenticating replays, but never let a later status silently reopen it.
	if ir_data.get(SETTLEMENT_CONFLICT_DATA_KEY):
		if status == "chargeback":
			_mark_locked_chargeback(ir.name, transaction, settings_name=settings_name)
		return {"ok": True}

	# Confirmation is terminal except for what happens to the money afterwards:
	# a chargeback, a dispute, or a refund Payrexx issued from its own dashboard.
	# Every other delayed or replayed state is ignored without replacing the
	# confirmed transaction evidence.
	if ir.status == "Completed" and status not in POST_SETTLEMENT_STATUSES:
		return {"ok": True}

	if status == "confirmed" and not expected_settings and _multiple_gateways_configured():
		frappe.log_error(
			title="Payrexx webhook unbound legacy request",
			message=frappe.as_json(
				_webhook_log_summary(transaction, reference_id, status) | {"verified_with": settings_name}
			),
		)
		return {"ok": True}

	with _payment_authorization_user(ir, settings_name):
		if status == "confirmed":
			_complete_locked_integration_request(ir.name, transaction, settings_name=settings_name)
		elif status in ("authorized", "reserved"):
			ir_data["payrexx_transaction"] = transaction
			ir.data = frappe.as_json(ir_data)
			ir.status = "Authorized"
			ir.save(ignore_permissions=True)
		elif status == "chargeback":
			_mark_locked_chargeback(ir.name, transaction, settings_name=settings_name)
		elif status in REFUND_STATUSES or status == "disputed":
			_record_reversal_evidence(ir, ir_data, transaction, status, settings_name=settings_name)
		elif status in ("cancelled", "declined", "error", "expired"):
			ir_data["payrexx_transaction"] = transaction
			ir.data = frappe.as_json(ir_data)
			ir.status = "Failed"
			ir.error = f"Payrexx status: {status}"
			ir.save(ignore_permissions=True)
		else:
			# 'waiting' and anything we don't recognise — keep listening.
			ir_data["payrexx_transaction"] = transaction
			ir.data = frappe.as_json(ir_data)
			ir.save(ignore_permissions=True)

	return {"ok": True}


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
	return _run_with_deadlock_retry(
		lambda: _reconcile_integration_request_once(integration_request_name, gateway_name)
	)


def _reconcile_integration_request_once(
	integration_request_name: str,
	gateway_name: str | None = None,
) -> bool:
	"""Run one complete reconciliation attempt; the public boundary owns retries."""
	if not integration_request_name or not frappe.db.exists("Integration Request", integration_request_name):
		return False

	ir = frappe.get_doc("Integration Request", integration_request_name)
	if ir.integration_request_service != "Payrexx":
		return False
	ir_data = frappe.parse_json(ir.data) or {}
	if _is_chargeback_recorded(ir, ir_data):
		return False
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
	# for requests that predate the stored gateway reference — and only on a
	# single-gateway site, where there is just one credential set anyway.
	stored_settings = ir_data.get("payrexx_settings") or _settings_name_from_request_data(ir_data)
	if not stored_settings and _multiple_gateways_configured():
		frappe.log_error(
			title="Payrexx reconcile unbound legacy request",
			message=f"Integration Request {ir.name} has no stored gateway binding; "
			"refusing caller-selected credentials on a multi-gateway site.",
		)
		return False
	settings = _resolve_settings(stored_settings or gateway_name)
	with _payment_authorization_user(ir, settings.name):
		gateway = settings._client().retrieve_gateway(int(gateway_id))
		transaction = _confirmed_transaction_from_gateway(gateway, ir.name)

		if transaction:
			_complete_locked_integration_request(ir.name, transaction, settings_name=settings.name)
			return frappe.db.get_value("Integration Request", ir.name, "status") == "Completed"
		status = (gateway.get("status") or "").lower()
		if status == "chargeback":
			_mark_locked_chargeback(ir.name, gateway, settings_name=settings.name)
		elif status in ("cancelled", "declined", "error", "expired"):
			_mark_reconciliation_failure(ir.name, status)
		return False


def _multiple_gateways_configured() -> bool:
	return frappe.db.count("Payrexx Settings") > 1


def _settings_name_from_request_data(ir_data: dict) -> str | None:
	payment_gateway = (ir_data.get("payment_gateway") or "").strip()
	if payment_gateway.startswith("Payrexx-"):
		return payment_gateway.removeprefix("Payrexx-")
	return None


def _confirmed_transaction_from_gateway(gateway: dict, expected_reference: str) -> dict:
	for invoice in gateway.get("invoices") or []:
		for transaction in invoice.get("transactions") or []:
			if (transaction.get("status") or "").lower() != "confirmed":
				continue
			provider_reference = (
				invoice.get("referenceId") or transaction.get("referenceId") or gateway.get("referenceId")
			)
			if provider_reference != expected_reference:
				continue
			amount = transaction.get("amount")
			if amount is None:
				amount = invoice.get("amount", gateway.get("amount"))
			transaction_invoice = transaction.get("invoice") or {}
			invoice_evidence = {
				key: transaction_invoice[key] if key in transaction_invoice else invoice.get(key)
				for key in ("referenceId", "currency", "test")
				if key in transaction_invoice or key in invoice
			}
			return {
				**transaction,
				"referenceId": provider_reference,
				"amount": amount,
				"currency": transaction.get("currency") or invoice.get("currency") or gateway.get("currency"),
				"invoice": invoice_evidence,
			}
	return {}


def _mark_reconciliation_failure(integration_request_name: str, status: str) -> None:
	integration_request = _get_current_locked_doc("Integration Request", integration_request_name)
	ir_data = frappe.parse_json(integration_request.data) or {}
	if (
		_is_chargeback_recorded(integration_request, ir_data)
		or ir_data.get(SETTLEMENT_CONFLICT_DATA_KEY)
		or integration_request.status == "Completed"
	):
		return
	integration_request.status = "Failed"
	integration_request.error = f"Payrexx status: {status}"
	integration_request.save(ignore_permissions=True)


def _complete_integration_request(integration_request_name: str, transaction: dict | None = None) -> None:
	"""Atomically record confirmation and settle its reference, retrying the whole unit."""
	_run_with_deadlock_retry(
		lambda: _complete_locked_integration_request(integration_request_name, transaction)
	)


def _complete_locked_integration_request(
	integration_request_name: str,
	transaction: dict | None = None,
	*,
	settings_name: str | None = None,
) -> None:
	integration_request = _get_current_locked_doc("Integration Request", integration_request_name)
	ir_data = frappe.parse_json(integration_request.data) or {}
	if _is_chargeback_recorded(integration_request, ir_data):
		return
	if ir_data.get(SETTLEMENT_CONFLICT_DATA_KEY):
		return
	if integration_request.status == "Completed":
		return

	with _payment_authorization_user(integration_request, settings_name):
		if transaction:
			ir_data["payrexx_transaction"] = transaction
		if conflict := _settlement_conflict(integration_request, ir_data, transaction or {}):
			_mark_settlement_conflict(integration_request, ir_data, conflict, settings_name=settings_name)
			return
		integration_request.data = frappe.as_json(ir_data)
		integration_request.status = "Completed"
		integration_request.error = ""
		integration_request.save(ignore_permissions=True)
		payment_entry_name = _on_payment_authorized(
			integration_request,
			"Completed",
			settings_name=settings_name,
		)
		if payment_entry_name:
			ir_data["payrexx_payment_entry"] = payment_entry_name
			integration_request.db_set("data", frappe.as_json(ir_data), update_modified=False)


def _on_payment_authorized(
	integration_request,
	status,
	*,
	settings_name: str | None = None,
) -> str | None:
	if not (integration_request.reference_doctype and integration_request.reference_docname):
		return None
	try:
		with _payment_authorization_user(integration_request, settings_name):
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
		frappe.log_error(title="Payrexx on_payment_authorized", message=frappe.get_traceback())
		raise
	return None


def _set_payment_request_as_paid(payment_request_name: str) -> str | None:
	payment_request = _get_current_locked_doc("Payment Request", payment_request_name)
	if payment_request.status == "Paid" or flt(payment_request.outstanding_amount) <= 0:
		return None
	payment_entry = payment_request.set_as_paid()
	return payment_entry.name if payment_entry else None


def _validate_sales_invoice_checkout_state(sales_invoice) -> tuple[int, str]:
	if sales_invoice.docstatus != 1 or sales_invoice.get("is_return"):
		frappe.throw(_("Payrexx requires a submitted non-return Sales Invoice."))

	currency = cstr(sales_invoice.get("currency")).strip().upper()
	if not currency:
		frappe.throw(_("The Sales Invoice currency is missing."))
	if flt(sales_invoice.get("outstanding_amount")) <= 0:
		frappe.throw(_("The Sales Invoice is no longer fully outstanding."))

	try:
		payable_amount = _canonical_gateway_amount(
			sales_invoice.get("rounded_total") or sales_invoice.get("grand_total"),
			currency,
		)
		outstanding_amount = _canonical_gateway_amount(sales_invoice.get("outstanding_amount"), currency)
	except ValueError as exc:
		frappe.throw(cstr(exc), frappe.ValidationError)
	if outstanding_amount != payable_amount:
		frappe.throw(
			_(
				"This Sales Invoice was partially paid or otherwise changed. "
				"Its original Payrexx checkout cannot be used."
			)
		)
	return outstanding_amount, currency


def _validate_payment_request_checkout_state(
	payment_request,
	sales_invoice,
	*,
	expected_gateway: str,
	requested_amount=None,
	requested_currency: str | None = None,
	require_submitted: bool,
) -> tuple[int, str]:
	expected_amount, invoice_currency = _validate_sales_invoice_checkout_state(sales_invoice)
	if (
		payment_request.reference_doctype != "Sales Invoice"
		or payment_request.reference_name != sales_invoice.name
	):
		frappe.throw(_("The Payment Request no longer identifies the expected Sales Invoice."))
	if payment_request.payment_request_type != "Inward":
		frappe.throw(_("Payrexx requires an inward Payment Request."))
	if payment_request.payment_gateway != expected_gateway:
		frappe.throw(_("The Payment Request no longer uses the expected Payrexx gateway."))
	if cstr(payment_request.get("company")) != cstr(sales_invoice.get("company")):
		frappe.throw(_("The Payment Request company no longer matches the Sales Invoice."))

	payment_currency = cstr(payment_request.get("currency")).strip().upper()
	if payment_currency != invoice_currency:
		frappe.throw(_("The Payment Request currency no longer matches the Sales Invoice."))
	if requested_currency and cstr(requested_currency).strip().upper() != payment_currency:
		frappe.throw(_("The checkout currency no longer matches the Payment Request."))

	try:
		payment_amount = _canonical_gateway_amount(payment_request.get("grand_total"), payment_currency)
		if requested_amount is not None:
			request_amount = _canonical_gateway_amount(requested_amount, payment_currency)
		else:
			request_amount = payment_amount
	except ValueError as exc:
		frappe.throw(cstr(exc), frappe.ValidationError)
	if payment_amount != expected_amount or request_amount != payment_amount:
		frappe.throw(_("The Payment Request amount no longer matches the fully outstanding Sales Invoice."))

	if require_submitted:
		if payment_request.docstatus != 1 or payment_request.status != "Requested":
			frappe.throw(_("Only a submitted Requested Payment Request can use this Payrexx checkout."))
	else:
		if payment_request.docstatus not in (0, 1):
			frappe.throw(_("The Payment Request is cancelled."))
		if payment_request.docstatus == 1 and payment_request.status != "Requested":
			frappe.throw(_("Only a Requested Payment Request can create a Payrexx checkout."))

	if payment_request.docstatus == 1:
		try:
			payment_outstanding = _canonical_gateway_amount(
				payment_request.get("outstanding_amount"),
				payment_currency,
			)
		except ValueError as exc:
			frappe.throw(cstr(exc), frappe.ValidationError)
		if payment_outstanding != payment_amount:
			frappe.throw(_("The Payment Request is partially paid or otherwise changed."))
	return payment_amount, payment_currency


def _canonical_gateway_amount(amount, currency: str) -> int:
	"""Convert a two-decimal checkout amount to Payrexx's canonical integer unit."""
	currency = cstr(currency).strip().upper()
	_validate_gateway_currency(currency)
	try:
		decimal_amount = Decimal(str(amount))
	except DECIMAL_CONVERSION_ERRORS:
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
	except DECIMAL_CONVERSION_ERRORS:
		raise ValueError(_("Provider amount is invalid."))
	if not decimal_amount.is_finite() or decimal_amount != decimal_amount.to_integral_value():
		raise ValueError(_("Provider amount must be an integer in the smallest currency unit."))
	return int(decimal_amount)


def _conflict(code: str, reason: str, evidence: dict | None = None) -> dict:
	return {"code": code, "reason": reason, "evidence": evidence or {}}


def _transaction_is_test(transaction: dict) -> bool:
	"""Whether Payrexx marks this as a simulated payment.

	``mode`` is the authoritative marker (``LIVE`` / ``TEST``); ``invoice.test``
	carries the same fact as 1/0. When neither field is present we cannot tell,
	and answering "not a test" keeps accounts that omit them settling exactly as
	they did before.
	"""
	mode = cstr(transaction.get("mode")).strip().upper()
	if mode:
		return mode != "LIVE"
	return cint((transaction.get("invoice") or {}).get("test")) == 1


def _gateway_allows_test_transactions(ir_data: dict) -> bool:
	settings_name = ir_data.get("payrexx_settings") or _settings_name_from_request_data(ir_data)
	if not settings_name:
		return False
	return bool(frappe.db.get_value("Payrexx Settings", settings_name, "allow_test_transactions"))


def _settlement_conflict(integration_request, ir_data: dict, transaction: dict) -> dict | None:
	# A simulated payment moves no money. It is signed with the same key as a
	# real one and matches every amount/currency/company check below, so without
	# this gate a TEST transaction settles a real document.
	if _transaction_is_test(transaction) and not _gateway_allows_test_transactions(ir_data):
		return _conflict(
			"test_transaction",
			_("The provider confirmation is a TEST payment and this gateway settles live payments only."),
			{
				"mode": cstr(transaction.get("mode")).strip() or None,
				"invoice_test": (transaction.get("invoice") or {}).get("test"),
			},
		)

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

	if integration_request.reference_doctype != "Payment Request":
		extension_validation = _extension_source_validation(
			phase="settlement",
			integration_request=integration_request,
			ir_data=ir_data,
			transaction=transaction,
		)
		if extension_validation is True:
			return None
		if isinstance(extension_validation, dict):
			return extension_validation

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
	payment_request = _get_current_locked_doc("Payment Request", integration_request.reference_docname)
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
	reference_document = _get_current_locked_doc(
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


def _extension_source_validation(*, phase: str, **context) -> bool | dict | None:
	"""Delegate explicitly supported non-standard sources; no provider means fail closed."""
	for provider in frappe.get_hooks(SETTLEMENT_SOURCE_PROVIDER_HOOK):
		result = frappe.get_attr(provider)(phase=phase, **context)
		if result is not None:
			return result
	return None


def _mark_settlement_conflict(
	integration_request,
	ir_data: dict,
	conflict: dict,
	*,
	settings_name: str | None = None,
) -> None:
	if ir_data.get(SETTLEMENT_CONFLICT_DATA_KEY):
		return
	marker = {
		"version": SETTLEMENT_CONFLICT_VERSION,
		"terminal": True,
		"detected_at": now_datetime(),
		**conflict,
	}
	ir_data[SETTLEMENT_CONFLICT_DATA_KEY] = marker
	integration_request.db_set(
		{
			"data": frappe.as_json(ir_data),
			"status": "Failed",
			"error": conflict["reason"],
		}
	)
	_ensure_review_todo(
		integration_request,
		SETTLEMENT_CONFLICT_TODO_MARKER,
		f"{SETTLEMENT_CONFLICT_TODO_MARKER} {conflict['reason']}",
		settings_name=settings_name,
	)


def _mark_chargeback(
	integration_request_name: str,
	transaction: dict | None = None,
	*,
	settings_name: str | None = None,
) -> None:
	"""Apply a chargeback atomically when called outside callback or reconciliation."""
	_run_with_deadlock_retry(
		lambda: _mark_locked_chargeback(integration_request_name, transaction, settings_name=settings_name)
	)


def _mark_locked_chargeback(
	integration_request_name: str,
	transaction: dict | None = None,
	*,
	settings_name: str | None = None,
) -> None:
	integration_request = _get_current_locked_doc("Integration Request", integration_request_name)
	ir_data = frappe.parse_json(integration_request.data) or {}
	with _evidence_recording_user(integration_request, settings_name):
		chargeback_recorded = _is_chargeback_recorded(integration_request, ir_data)
		if transaction and not chargeback_recorded:
			ir_data["payrexx_transaction"] = transaction
		updates = {"status": "Failed", "error": CHARGEBACK_ERROR}
		if not chargeback_recorded:
			updates["data"] = frappe.as_json(ir_data)
		integration_request.db_set(updates)

	_ensure_review_todo(
		integration_request,
		CHARGEBACK_TODO_MARKER,
		f"{CHARGEBACK_TODO_MARKER} "
		+ _(
			"Manual accounting reversal required. Review the linked settlement; "
			"submitted ledger records were preserved."
		),
		settings_name=settings_name,
	)


def _subscription_reversal_targets_signup(ir_data: dict, transaction: dict, status: str) -> bool:
	settled = ir_data.get("payrexx_transaction") or {}
	if not settled:
		return False
	original_transaction = _reversed_transaction_key(transaction, status)
	return bool(original_transaction and original_transaction == _provider_event_key(settled))


def _reversed_transaction_key(transaction: dict, status: str) -> str:
	original = cstr(
		transaction.get("originalTransactionUuid") or transaction.get("originalTransactionId")
	).strip()
	if original:
		return original
	if status in ("chargeback", "disputed"):
		return cstr(transaction.get("uuid") or transaction.get("id")).strip()
	return ""


def _process_subscription_reversal(
	settings_name: str,
	transaction: dict,
	subscription: dict,
	reference_id: str,
	status: str,
) -> dict[str, bool | str]:
	"""Dispatch a later-installment reversal without mutating its signup request."""
	if status not in SUBSCRIPTION_REVERSAL_STATUS_STAGE:
		frappe.throw(
			_("Unsupported Payrexx subscription reversal status: {0}").format(status or _("blank")),
			frappe.ValidationError,
		)
	with as_automation_user(settings_name):
		if not webhook_payload.is_live(transaction) and not _gateway_allows_test_transactions(
			{"payrexx_settings": settings_name}
		):
			frappe.log_error(
				title="Payrexx subscription reversal ignored (TEST mode)",
				message=frappe.as_json(_subscription_log_summary(subscription) | {"reference": reference_id}),
			)
			return {"ok": True}

		event = _prepare_subscription_reversal_event(
			settings_name, subscription, transaction, reference_id, status
		)
		if not event:
			return {"ok": True}
		try:
			claimed = _dispatch_subscription_event(
				"reversal",
				subscription=subscription,
				transaction=transaction,
				reference_id=reference_id,
				status=status,
				settings_name=settings_name,
			)
		except frappe.QueryDeadlockError:
			raise
		except Exception:
			frappe.logger("payrexx_integration").exception("Payrexx subscription reversal provider failed")
			raise SubscriptionEventDispatchError from None
		event.db_set(
			{
				"dispatch_status": "Claimed" if claimed else "Unclaimed",
				"processed_on": now_datetime(),
			}
		)
		if not claimed:
			frappe.log_error(
				title="Payrexx subscription reversal unclaimed",
				message=frappe.as_json(_subscription_log_summary(subscription) | {"reference": reference_id}),
			)
			if getattr(frappe.local, "request", None):
				frappe.local.response["http_status_code"] = 503
			return {"ok": False, "error": "subscription_event_unclaimed"}
	return {"ok": True}


def _process_subscription_charge(
	settings_name: str,
	transaction: dict,
	subscription: dict,
	reference_id: str,
	status: str,
	*,
	integration_request: Document | None,
) -> dict[str, bool | str] | None:
	"""Decide whether a subscription charge is the signup or a later installment.

	Payrexx echoes the same ``referenceId`` on every charge, because it was set
	once when the Gateway was created. So a monthly donor's twelfth payment
	arrives pointing at the Integration Request that settled their first one.
	Without this split, that request's own terminality would silently discard
	eleven real payments.

	The rule is the request's state, not the reference:

	* no request, or a Completed one — a later installment. The owning app
	  records it; this app settles nothing, because the checkout it was created
	  for is long finished.
	* any other state — the signup charge itself. Returns ``None`` so the
	  ordinary settlement path runs untouched, including every terminal guard.

	Returning ``None`` is what keeps one-off behaviour and first-charge
	behaviour identical: there is no second settlement implementation.
	"""
	if integration_request and integration_request.status != "Completed":
		return None
	if status not in SUBSCRIPTION_INSTALLMENT_STATUSES:
		frappe.throw(
			_("Unsupported Payrexx subscription transaction status: {0}").format(status or _("blank")),
			frappe.ValidationError,
		)

	# Guard the boundary between the two: a replay of the signup charge, after
	# that charge settled, must not be recorded a second time as an installment.
	if integration_request:
		settled = (frappe.parse_json(integration_request.data) or {}).get("payrexx_transaction") or {}
		if settled and _provider_event_key(settled) == _provider_event_key(transaction):
			return {"ok": True}

	with as_automation_user(settings_name):
		if not webhook_payload.is_live(transaction) and not _gateway_allows_test_transactions(
			{"payrexx_settings": settings_name}
		):
			frappe.log_error(
				title="Payrexx subscription charge ignored (TEST mode)",
				message=frappe.as_json(_subscription_log_summary(subscription) | {"reference": reference_id}),
			)
			return {"ok": True}

		event = _prepare_subscription_installment_event(
			settings_name, subscription, transaction, reference_id, status
		)
		if not event:
			return {"ok": True}

		try:
			claimed = _dispatch_subscription_event(
				"charge",
				subscription=subscription,
				transaction=transaction,
				reference_id=reference_id,
				status=status,
				settings_name=settings_name,
			)
		except frappe.QueryDeadlockError:
			raise
		except Exception:
			frappe.logger("payrexx_integration").exception("Payrexx subscription charge provider failed")
			raise SubscriptionEventDispatchError from None
		event.db_set(
			{
				"dispatch_status": "Claimed" if claimed else "Unclaimed",
				"processed_on": now_datetime(),
			}
		)
		if not claimed:
			frappe.log_error(
				title="Payrexx subscription charge unclaimed",
				message=frappe.as_json(_subscription_log_summary(subscription) | {"reference": reference_id}),
			)
			# Persist the Unclaimed state but return a retryable HTTP response. Raising
			# here would roll the durable row back with the request and remove the
			# operator-visible redrive state we just recorded.
			if getattr(frappe.local, "request", None):
				frappe.local.response["http_status_code"] = 503
			return {"ok": False, "error": "subscription_event_unclaimed"}
	return {"ok": True}


def _prepare_subscription_installment_event(
	settings_name: str,
	subscription: dict,
	transaction: dict,
	reference_id: str,
	status: str,
) -> Document | None:
	"""Lock or insert the monotonic durable state for one recurring transaction.

	A claimed replay of the same status is terminal. A later provider status may
	advance the row, and any Unclaimed/Processing status is retryable. The row and
	provider effects remain in one transaction.
	"""
	return _prepare_subscription_financial_event(
		settings_name,
		subscription,
		transaction,
		reference_id,
		status,
		event_type="Installment",
		event_key_type="charge",
		status_stages=SUBSCRIPTION_INSTALLMENT_STATUS_STAGE,
	)


def _prepare_subscription_reversal_event(
	settings_name: str,
	subscription: dict,
	transaction: dict,
	reference_id: str,
	status: str,
) -> Document | None:
	return _prepare_subscription_financial_event(
		settings_name,
		subscription,
		transaction,
		reference_id,
		status,
		event_type="Reversal",
		event_key_type="reversal",
		status_stages=SUBSCRIPTION_REVERSAL_STATUS_STAGE,
	)


def _prepare_subscription_financial_event(
	settings_name: str,
	subscription: dict,
	transaction: dict,
	reference_id: str,
	status: str,
	*,
	event_type: str,
	event_key_type: str,
	status_stages: dict[str, int],
) -> Document | None:
	provider_event_id = _provider_event_key(transaction)
	event_key = hashlib.sha256(
		f"{settings_name}\x00{event_key_type}\x00{provider_event_id}".encode()
	).hexdigest()
	redrive_payload = frappe.as_json(_subscription_redrive_payload(subscription, transaction))
	try:
		event = _get_current_locked_doc(SUBSCRIPTION_EVENT_DOCTYPE, event_key)
	except frappe.DoesNotExistError:
		event = None
	if event:
		if not _subscription_event_status_advances(event, status, status_stages):
			return None
		event.db_set(
			{
				"provider_status": status,
				"dispatch_status": "Processing",
				"processed_on": None,
				"redrive_payload": redrive_payload,
			}
		)
		return event
	try:
		return frappe.get_doc(
			{
				"doctype": SUBSCRIPTION_EVENT_DOCTYPE,
				"event_key": event_key,
				"event_type": event_type,
				"payrexx_settings": settings_name,
				"subscription_id": webhook_payload.subscription_id(subscription),
				"reference_id": reference_id,
				"provider_event_id": provider_event_id,
				"provider_status": status,
				"dispatch_status": "Processing",
				"redrive_payload": redrive_payload,
			}
		).insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		# A concurrent callback won the insert. Re-read its committed current state:
		# a Claimed same-status winner is terminal, while an Unclaimed winner must
		# not let this request answer 200 and stop provider retries.
		event = _get_current_locked_doc(SUBSCRIPTION_EVENT_DOCTYPE, event_key)
		if not _subscription_event_status_advances(event, status, status_stages):
			return None
		event.db_set(
			{
				"provider_status": status,
				"dispatch_status": "Processing",
				"processed_on": None,
				"redrive_payload": redrive_payload,
			}
		)
		return event


def _subscription_installment_status_advances(event: Document, status: str) -> bool:
	return _subscription_event_status_advances(event, status, SUBSCRIPTION_INSTALLMENT_STATUS_STAGE)


def _subscription_event_status_advances(
	event: Document,
	status: str,
	status_stages: dict[str, int],
) -> bool:
	previous = cstr(event.get("provider_status")).strip().lower()
	if not previous:
		return True
	if previous == status:
		return event.get("dispatch_status") != "Claimed"
	if event.get("event_type") == "Reversal" and previous != "refund_pending":
		return False
	return status_stages[status] >= status_stages.get(previous, -1)


def _subscription_redrive_payload(subscription: dict, transaction: dict) -> dict:
	"""Persist only the non-PII financial evidence an owning hook needs to retry."""
	invoice = transaction.get("invoice") or {}
	return {
		"subscription": {
			"id": webhook_payload.subscription_id(subscription),
			"status": webhook_payload.subscription_status(subscription),
			"valid_until": webhook_payload.subscription_next_payment(subscription),
			"paymentInterval": webhook_payload.subscription_interval(subscription),
		},
		"transaction": {
			"uuid": transaction.get("uuid"),
			"id": transaction.get("id"),
			"status": webhook_payload.transaction_status(transaction),
			"mode": transaction.get("mode"),
			"time": transaction.get("time"),
			"amount": transaction.get("amount"),
			"currency": transaction.get("currency"),
			"originalTransactionId": transaction.get("originalTransactionId"),
			"originalTransactionUuid": transaction.get("originalTransactionUuid"),
			"invoice": {
				"referenceId": invoice.get("referenceId"),
				"currency": invoice.get("currency"),
				"test": invoice.get("test"),
			},
		},
	}


def _persist_unclaimed_subscription_event(
	settings_name: str,
	transaction: dict,
	reference_id: str,
	status: str,
) -> None:
	subscription = webhook_payload.embedded_subscription(transaction)
	prepare_event = (
		_prepare_subscription_reversal_event
		if status in SUBSCRIPTION_REVERSAL_STATUS_STAGE
		else _prepare_subscription_installment_event
	)
	event = prepare_event(settings_name, subscription, transaction, reference_id, status)
	if event:
		event.db_set({"dispatch_status": "Unclaimed", "processed_on": now_datetime()})


def _redrive_unclaimed_subscription_events(
	settings: Document,
	*,
	commit_each: bool = False,
) -> dict[str, int]:
	"""Replay locally retained financial events before status reconciliation."""
	event_names = frappe.get_all(
		SUBSCRIPTION_EVENT_DOCTYPE,
		filters={"payrexx_settings": settings.name, "dispatch_status": "Unclaimed"},
		pluck="name",
		order_by="creation asc",
	)
	retried = claimed = failed = 0
	for event_name in event_names:
		retried += 1
		savepoint = f"payrexx_subscription_redrive_{retried}"
		if not commit_each:
			frappe.db.savepoint(savepoint)
		try:
			was_claimed = _redrive_subscription_event(event_name, settings.name)
		except frappe.QueryDeadlockError:
			raise
		except frappe.QueryTimeoutError:
			raise
		except Exception:
			failed += 1
			if commit_each:
				frappe.db.rollback()
			else:
				frappe.db.rollback(save_point=savepoint)
			frappe.log_error(
				title="Payrexx subscription event redrive failed",
				message=frappe.as_json({"event": event_name, "gateway": settings.name}),
			)
			if commit_each:
				frappe.db.commit()  # nosemgrep: frappe-manual-commit
			continue
		else:
			if was_claimed:
				claimed += 1
			if commit_each:
				frappe.db.commit()  # nosemgrep: frappe-manual-commit
			else:
				frappe.db.release_savepoint(savepoint)
	return {"retried": retried, "claimed": claimed, "failed": failed}


def _redrive_subscription_event(event_name: str, settings_name: str) -> bool:
	event = _get_current_locked_doc(SUBSCRIPTION_EVENT_DOCTYPE, event_name)
	if event.dispatch_status != "Unclaimed" or event.payrexx_settings != settings_name:
		return False
	payload = frappe.parse_json(event.redrive_payload) or {}
	subscription = payload.get("subscription") or {}
	transaction = payload.get("transaction") or {}
	payload_provider_id = cstr(transaction.get("uuid") or transaction.get("id")).strip()
	if (
		webhook_payload.subscription_id(subscription) != cstr(event.subscription_id)
		or (payload_provider_id and payload_provider_id != event.provider_event_id)
		or webhook_payload.transaction_status(transaction) != event.provider_status
	):
		frappe.throw(_("Payrexx subscription event {0} has invalid redrive evidence.").format(event.name))

	event.db_set({"dispatch_status": "Processing", "processed_on": None})
	dispatch_event = "reversal" if event.event_type == "Reversal" else "charge"
	claimed = _dispatch_subscription_event(
		dispatch_event,
		subscription=subscription,
		transaction=transaction,
		reference_id=event.reference_id,
		status=event.provider_status,
		settings_name=settings_name,
	)
	event.db_set(
		{
			"dispatch_status": "Claimed" if claimed else "Unclaimed",
			"processed_on": now_datetime(),
		}
	)
	return claimed


def _enqueue_subscription_transaction_reconciliation(settings_name: str, subscription: dict) -> None:
	"""Recover the charge behind a lifecycle webhook after this request commits."""
	subscription_id = webhook_payload.subscription_id(subscription)
	reference_id = webhook_payload.reference_id(subscription)
	job_scope = hashlib.sha256(
		f"{settings_name}\x00{subscription_id}\x00{reference_id}".encode()
	).hexdigest()[:24]
	frappe.enqueue(
		"payrexx_integration.payrexx_integration.doctype.payrexx_settings."
		"payrexx_settings.reconcile_subscription_transactions",
		queue="long",
		timeout=900,
		job_id=f"payrexx-subscription-transactions:{job_scope}",
		deduplicate=True,
		enqueue_after_commit=True,
		gateway_name=settings_name,
		subscription_id=subscription_id,
		reference_id=reference_id,
	)


def reconcile_subscription_transactions(
	gateway_name: str,
	subscription_id: str,
	reference_id: str = "",
) -> dict[str, int]:
	"""Recover recent transactions for one lifecycle webhook without inline provider I/O."""
	settings = _resolve_settings(gateway_name)
	window_end = _utc_now()
	window_start = window_end - TRANSACTION_RECONCILIATION_INITIAL_LOOKBACK
	with as_automation_user(settings):
		return _reconcile_settings_transactions(
			settings,
			window_start=window_start,
			window_end=window_end,
			subscription_id=subscription_id,
			reference_id=reference_id,
			commit_each=not frappe.flags.in_test,
		)


def _reconcile_settings_transactions_with_cursor(
	settings: Document,
	*,
	commit_each: bool,
) -> dict[str, int]:
	now_utc = _utc_now()
	cursor = _utc_datetime(settings.get("transaction_reconciliation_cursor"))
	if cursor and cursor <= now_utc:
		window_start = cursor - TRANSACTION_RECONCILIATION_OVERLAP
		window_end = min(now_utc, cursor + TRANSACTION_RECONCILIATION_MAX_WINDOW)
	else:
		window_end = now_utc
		window_start = window_end - TRANSACTION_RECONCILIATION_INITIAL_LOOKBACK

	result = _reconcile_settings_transactions(
		settings,
		window_start=window_start,
		window_end=window_end,
		commit_each=commit_each,
	)
	if not result["failed"]:
		settings.db_set("transaction_reconciliation_cursor", window_end, update_modified=False)
		if commit_each:
			frappe.db.commit()  # nosemgrep: frappe-manual-commit
	return result


def _reconcile_settings_transactions(
	settings: Document,
	*,
	window_start,
	window_end,
	subscription_id: str = "",
	reference_id: str = "",
	commit_each: bool = False,
) -> dict[str, int]:
	"""Page through real provider transactions and replay only subscription money."""
	client = settings._client()
	target_subscription = cstr(subscription_id).strip()
	target_reference = cstr(reference_id).strip()
	seen = processed = failed = 0
	offset = 0
	for page_number in range(1, TRANSACTION_RECONCILIATION_MAX_PAGES + 1):
		page = client.list_transactions(
			datetime_utc_greater_than=_utc_query_value(window_start),
			datetime_utc_less_than=_utc_query_value(window_end),
			my_transactions_only=True,
			order_by_time="ASC",
			offset=offset,
			limit=TRANSACTION_RECONCILIATION_PAGE_SIZE,
		)
		if not page:
			break
		for transaction in page:
			if not isinstance(transaction, dict):
				continue
			subscription = webhook_payload.embedded_subscription(transaction)
			if not subscription:
				continue
			transaction_subscription = webhook_payload.subscription_id(subscription)
			transaction_reference = webhook_payload.reference_id(transaction)
			if target_subscription and transaction_subscription != target_subscription:
				continue
			if target_reference and transaction_reference != target_reference:
				continue

			seen += 1
			savepoint = f"payrexx_transaction_reconciliation_{seen}"
			if not commit_each:
				frappe.db.savepoint(savepoint)
			try:
				result = _process_callback_transaction(
					settings.name,
					transaction,
					transaction_reference,
					webhook_payload.transaction_status(transaction),
				)
			except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):  # fmt: skip
				raise
			except Exception:
				failed += 1
				if commit_each:
					frappe.db.rollback()
				else:
					frappe.db.rollback(save_point=savepoint)
				frappe.log_error(
					title="Payrexx transaction reconciliation event failed",
					message=frappe.as_json(
						_webhook_log_summary(
							transaction,
							transaction_reference,
							webhook_payload.transaction_status(transaction),
						)
						| {"gateway": settings.name}
					),
				)
			else:
				if result.get("ok") is True:
					processed += 1
				else:
					failed += 1
				if not commit_each:
					frappe.db.release_savepoint(savepoint)
			if commit_each:
				frappe.db.commit()  # nosemgrep: frappe-manual-commit

		if len(page) < TRANSACTION_RECONCILIATION_PAGE_SIZE:
			break
		if page_number == TRANSACTION_RECONCILIATION_MAX_PAGES:
			frappe.throw(
				_("Payrexx transaction reconciliation exceeded its bounded page limit."),
				frappe.ValidationError,
			)
		offset += TRANSACTION_RECONCILIATION_PAGE_SIZE
	return {"seen": seen, "processed": processed, "failed": failed}


def _utc_now() -> datetime:
	return datetime.now(UTC).replace(tzinfo=None)


def _utc_datetime(value) -> datetime | None:
	if not value:
		return None
	parsed = get_datetime(value)
	if parsed.tzinfo:
		return parsed.astimezone(UTC).replace(tzinfo=None)
	return parsed


def _utc_query_value(value) -> str:
	parsed = _utc_datetime(value)
	if not parsed:
		raise ValueError("A UTC transaction reconciliation boundary is required")
	return parsed.strftime("%Y-%m-%d %H:%M:%S")


def enqueue_subscription_reconciliation() -> dict[str, int]:
	"""Queue one deduplicated reconciliation worker per Payrexx Settings row."""
	settings_names = frappe.get_all("Payrexx Settings", pluck="name", order_by="name asc")
	for settings_name in settings_names:
		frappe.enqueue(
			"payrexx_integration.payrexx_integration.doctype.payrexx_settings."
			"payrexx_settings.reconcile_subscriptions",
			queue="long",
			timeout=3600,
			job_id=f"payrexx-subscription-reconciliation:{settings_name}",
			deduplicate=True,
			gateway_name=settings_name,
		)
	return {"gateways": len(settings_names)}


def reconcile_subscriptions(gateway_name: str) -> dict[str, int]:
	"""Recover one gateway's transactions and replay current subscription state.

	Webhook delivery is not a guarantee. A subscription that silently stops —
	because a delivery was lost while our site was down past Payrexx's retry
	window — is invisible revenue loss, so the provider is asked directly rather
	than trusted to have told us.

	The transaction pass processes authenticated provider records, while the
	subscription list remains a status-only reporting pass.
	"""
	settings = _resolve_settings(gateway_name)
	commit_each = not frappe.flags.in_test
	with as_automation_user(settings):
		_redrive_unclaimed_subscription_events(settings, commit_each=commit_each)
		_reconcile_settings_transactions_with_cursor(settings, commit_each=commit_each)
		return _reconcile_settings_subscriptions(
			settings,
			commit_each=commit_each,
			redrive_unclaimed=False,
		)


def _reconcile_settings_subscriptions(
	settings: Document,
	*,
	commit_each: bool = False,
	redrive_unclaimed: bool = True,
) -> dict[str, int]:
	"""Replay the current subscription state for one explicitly owned gateway."""
	# Payrexx rate-limits at the edge (600 / 5 min) and the client backs off, but
	# the sweep is still the caller most likely to meet it — page rather than
	# pull the whole instance in one request.
	with as_automation_user(settings):
		if redrive_unclaimed:
			_redrive_unclaimed_subscription_events(settings, commit_each=commit_each)
		client = settings._client()
		seen, claimed, failed = 0, 0, 0
		offset, page_size = 0, 100
		while True:
			page = client.list_subscriptions(offset=offset, limit=page_size)
			if not page:
				break
			for subscription in page:
				seen += 1
				savepoint = f"payrexx_subscription_reconciliation_{seen}"
				if not commit_each:
					frappe.db.savepoint(savepoint)
				try:
					was_claimed = _dispatch_subscription_event(
						"status", subscription=subscription, settings_name=settings.name
					)
				except Exception:
					failed += 1
					if commit_each:
						frappe.db.rollback()
					else:
						frappe.db.rollback(save_point=savepoint)
					frappe.log_error(
						title="Payrexx subscription reconciliation event failed",
						message=frappe.as_json(
							_subscription_log_summary(subscription) | {"gateway": settings.name}
						),
					)
					if commit_each:
						frappe.db.commit()  # nosemgrep: frappe-manual-commit
					continue
				else:
					if was_claimed:
						claimed += 1
					if commit_each:
						frappe.db.commit()  # nosemgrep: frappe-manual-commit
					else:
						frappe.db.release_savepoint(savepoint)
			if len(page) < page_size:
				break
			offset += page_size

	if seen and not claimed:
		# Every subscription unclaimed means the owning app is missing or its
		# hook is misconfigured — not that nothing changed.
		frappe.log_error(
			title="Payrexx subscription reconciliation claimed nothing",
			message=frappe.as_json({"gateway": settings.name, "subscriptions": seen}),
		)
	return {"subscriptions": seen, "claimed": claimed, "failed": failed}


def _dispatch_subscription_event(event: str, **context) -> bool:
	"""Hand a subscription event to the app that owns the instruction."""
	for provider in frappe.get_hooks(SUBSCRIPTION_EVENT_PROVIDER_HOOK):
		if frappe.get_attr(provider)(event=event, **context) is True:
			return True
	return False


def _subscription_log_summary(subscription: dict) -> dict:
	"""Non-PII evidence only, matching the transaction webhook log contract."""
	return {
		"subscription": webhook_payload.subscription_id(subscription),
		"status": webhook_payload.subscription_status(subscription),
		"valid_until": webhook_payload.subscription_next_payment(subscription),
		"interval": webhook_payload.subscription_interval(subscription),
	}


def _provider_event_key(transaction: dict) -> str:
	"""Stable identity for one provider event, so a replayed delivery is not re-recorded.

	A refund is its own transaction with its own id, which is what makes two
	genuine partial refunds distinguishable from ten deliveries of one. When the
	provider sends neither id nor uuid, a digest of the payload keeps replays
	idempotent — Payrexx retries the same body, so the same body means the same
	event.
	"""
	identifier = cstr(transaction.get("uuid") or transaction.get("id") or "").strip()
	if identifier:
		return identifier
	payload = json.dumps(transaction, sort_keys=True, default=str)
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _record_reversal_evidence(
	integration_request,
	ir_data: dict,
	transaction: dict,
	status: str,
	*,
	settings_name: str | None = None,
) -> None:
	"""Record a provider-side refund or dispute as evidence. Posts nothing to the ledger.

	Refunds are issued in the Payrexx dashboard, so ERPNext's job is to show that
	it happened and put it in front of accounting — not to reverse anything by
	itself. The entry is appended to its own list and never overwrites
	``payrexx_transaction``: the original confirmed settlement stays the record
	of what was collected, and the reversal sits beside it.

	The Integration Request status is deliberately left alone. A refunded payment
	did settle; rewriting it to Failed would misstate the history that
	reconciliation reads.
	"""
	key = _provider_event_key(transaction)
	reversals = ir_data.setdefault(REVERSAL_DATA_KEY, [])
	invoice = transaction.get("invoice") or {}
	new_values = {
		"key": key,
		"status": status,
		"amount": transaction.get("amount"),
		"currency": cstr(invoice.get("currency") or transaction.get("currency")).upper() or None,
		"original_transaction": transaction.get("originalTransactionId")
		or transaction.get("originalTransactionUuid"),
		"recorded_at": now_datetime(),
	}
	entry = next((item for item in reversals if item.get("key") == key), None)
	if entry:
		if entry.get("status") == status:
			return
		if entry.get("status") != "refund_pending":
			return
		# The provider commonly advances one refund transaction from pending to
		# its final status. Update that event in place, then raise the notification
		# exactly once; treating the key as wholly terminal would lose the final
		# accounting alert.
		entry.update(new_values)
	else:
		entry = new_values
		reversals.append(entry)
	with _evidence_recording_user(integration_request, settings_name):
		integration_request.db_set({"data": frappe.as_json(ir_data)})

	# A pending refund has not moved money yet; there is nothing for accounting
	# to act on until it lands, and a ToDo now would only be noise.
	if status == "refund_pending":
		return

	is_dispute = status == "disputed"
	marker = f"{DISPUTE_TODO_MARKER if is_dispute else REFUND_TODO_MARKER} {key}"
	if is_dispute:
		description = _("Payer disputed this payment. A chargeback may follow; review the settlement.")
	else:
		description = _(
			"Payrexx refunded this payment ({0}). Post the accounting reversal manually — "
			"no ledger entry was changed."
		).format(_reversal_amount_label(entry))
	_ensure_review_todo(integration_request, marker, f"{marker} {description}", settings_name=settings_name)
	_add_reversal_notice(integration_request, entry, description, settings_name=settings_name)


def _reversal_amount_label(entry: dict) -> str:
	"""Provider amounts are in the currency's smallest unit."""
	amount = entry.get("amount")
	if amount is None:
		return cstr(entry.get("status"))
	return f"{entry.get('currency') or ''} {flt(amount) / 100:.2f}".strip()


def _add_reversal_notice(
	integration_request,
	entry: dict,
	description: str,
	*,
	settings_name: str | None = None,
) -> None:
	"""Leave the evidence where finance actually looks: on the paid document.

	Extensions own their own reference types (a Donation is not this app's
	business), so a registered provider handling the reference wins. The standard
	Payment Request -> Sales Invoice chain is handled here, because this app
	already understands it.
	"""
	with _evidence_recording_user(integration_request, settings_name):
		for provider in frappe.get_hooks(REFUND_NOTICE_PROVIDER_HOOK):
			if frappe.get_attr(provider)(integration_request=integration_request, reversal=entry) is True:
				return

		if integration_request.reference_doctype != "Payment Request":
			return
		invoice_name = frappe.db.get_value(
			"Payment Request", integration_request.reference_docname, "reference_name"
		)
		if not invoice_name or not frappe.db.exists("Sales Invoice", invoice_name):
			return
		frappe.get_doc("Sales Invoice", invoice_name).add_comment("Comment", description)


def _is_chargeback_recorded(integration_request, ir_data: dict | None = None) -> bool:
	ir_data = ir_data if ir_data is not None else frappe.parse_json(integration_request.data) or {}
	transaction = ir_data.get("payrexx_transaction") or {}
	return (transaction.get("status") or "").lower() == "chargeback" or cstr(
		integration_request.get("error")
	) == CHARGEBACK_ERROR


def _ensure_review_todo(
	integration_request,
	marker: str,
	description: str,
	*,
	settings_name: str | None = None,
) -> None:
	"""Idempotently create the High-priority review ToDo for an Integration Request."""
	with _evidence_recording_user(integration_request, settings_name):
		if frappe.db.exists(
			"ToDo",
			{
				"reference_type": "Integration Request",
				"reference_name": integration_request.name,
				"description": ["like", f"{marker}%"],
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
				"description": description,
			}
		).insert(ignore_permissions=True)


def _payment_authorization_user(integration_request, settings_name: str | None = None):
	from payrexx_integration.session_utils import as_automation_user

	ir_data = frappe.parse_json(integration_request.get("data")) or {}
	stored_settings = ir_data.get("payrexx_settings") or _settings_name_from_request_data(ir_data)
	return as_automation_user(stored_settings or settings_name or _resolve_settings().name)


def _evidence_recording_user(integration_request, settings_name: str | None = None):
	"""Privilege switch for terminal-evidence writes (chargebacks, review ToDos).

	Settlement stays fail-closed via ``_payment_authorization_user`` — money must
	never move without the owning gateway's automation user. Recording chargeback
	or conflict *evidence* is different: for a request with no stored gateway
	binding on a site with zero or multiple Payrexx Settings rows, gateway
	resolution is impossible, and throwing here would discard the evidence and
	its review ToDo. Degrade instead: log the anomaly and record the evidence as
	the current session user.
	"""
	try:
		return _payment_authorization_user(integration_request, settings_name)
	except frappe.ValidationError:
		frappe.log_error(
			title="Payrexx evidence recorded without gateway automation user",
			message=(
				f"Integration Request {integration_request.name}: no Payrexx Settings row "
				"could be resolved (unbound request on a zero- or multi-gateway site). "
				f"Terminal evidence was recorded as {frappe.session.user}.\n\n" + frappe.get_traceback()
			),
		)
		return nullcontext()
