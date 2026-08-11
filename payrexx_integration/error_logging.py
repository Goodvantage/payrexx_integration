# Copyright (c) 2026, Goodvantage GmbH and contributors

"""Context-free Error Log boundary for Payrexx failures.

Deliberate self-contained twin (D46): payrexx_integration stays standalone on
top of upstream `payments` and must not import good_connector. The shared
engine is `good_connector.error_logging`; this module's contract — re-raise
transaction errors before any logging side effect, defer on read-only/safe
methods, guard the no-DB context, file-log fallback — is pinned against it by
good_connector's `tests/test_error_logging_parity.py`.
"""

from __future__ import annotations

from hashlib import sha256

import frappe
from frappe.utils import cstr

TRANSACTION_ERRORS = (frappe.QueryDeadlockError, frappe.QueryTimeoutError)
SAFE_HTTP_METHODS = {"GET", "HEAD", "OPTIONS"}
ERROR_SUMMARY = "Provider data and traceback context were omitted."


def log_sanitized_error(
	operation: str,
	exception: BaseException,
	*,
	http_status: int | None = None,
) -> str:
	"""Insert one bounded Error Log without invoking core traceback/Sentry capture."""
	if isinstance(exception, TRANSACTION_ERRORS):
		raise exception

	operation = _bounded_identifier(operation, 80)
	exception_class = _bounded_identifier(type(exception).__name__, 80)
	status = _bounded_identifier(http_status if http_status is not None else "None", 16)
	message = "\n".join(
		(
			f"operation={operation}",
			f"exception_class={exception_class}",
			f"http_status={status}",
			f"summary={ERROR_SUMMARY}",
		)
	)
	if not getattr(frappe.local, "db", None):
		_log_to_file(message)
		return message

	values = {
		"doctype": "Error Log",
		"method": _error_title(operation),
		"error": message,
		"metadata": "{}",
		"fingerprint": error_fingerprint(operation, exception_class),
	}
	try:
		doc = frappe.get_doc(values)
		if _must_defer_database_log():
			doc.deferred_insert()
		else:
			doc.insert(ignore_permissions=True)
	except TRANSACTION_ERRORS:
		raise
	except Exception:
		_log_to_file(f"{message}\ndatabase_error_log=failed")
	return message


def error_fingerprint(operation: str, exception_class: str) -> str:
	identity = (
		f"payrexx_integration:{_bounded_identifier(operation, 80)}:{_bounded_identifier(exception_class, 80)}"
	)
	return sha256(identity.encode()).hexdigest()


def _error_title(operation: str) -> str:
	titles = {
		"payrexx_pay_url": "Payrexx pay URL unavailable",
		"payrexx_request": "Payrexx request failed",
		"payrexx_response": "Payrexx response failed",
	}
	return titles.get(operation, f"Payrexx {operation} failed")[:140]


def _must_defer_database_log() -> bool:
	request = getattr(frappe.local, "request", None)
	return bool(
		frappe.flags.read_only
		or (request and cstr(getattr(request, "method", "")).upper() in SAFE_HTTP_METHODS)
	)


def _log_to_file(message: str) -> None:
	try:
		frappe.logger("payrexx_integration", allow_site=True).error(message.replace("\n", " | "))
	except Exception:
		pass


def _bounded_identifier(value, length: int) -> str:
	return " ".join(cstr(value).split())[:length]
