# Copyright (c) 2026, Goodvantage GmbH and contributors

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from payrexx_integration import error_logging


class TestSanitizedErrorLogging(IntegrationTestCase):
	def test_real_boundary_omits_exception_request_and_sentry_context(self) -> None:
		api_secret = "sk_live_direct_boundary_secret"
		payer_email = "direct-boundary-payer@example.test"
		provider_url = "https://api.payrexx.com/v1.16/Gateway/?token=provider-private"
		provider_response = '{"error":"declined","credential":"' + api_secret + '"}'
		exception_text = f"provider rejected {payer_email} at {provider_url}: {provider_response}"
		fingerprint = error_logging.error_fingerprint("payrexx_request", "RuntimeError")
		before = set(frappe.get_all("Error Log", filters={"fingerprint": fingerprint}, pluck="name"))
		original_form_dict = frappe.local.form_dict
		frappe.local.form_dict = frappe._dict(
			payer_email=payer_email,
			provider_url=provider_url,
			api_secret=api_secret,
		)
		try:
			with (
				patch.object(error_logging, "_must_defer_database_log", return_value=False),
				patch.object(error_logging, "_log_to_file"),
				patch.object(frappe, "log_error") as core_log_error,
				patch.object(frappe, "get_traceback") as get_traceback,
				patch("frappe.utils.sentry.capture_exception") as capture_exception,
			):
				message = error_logging.log_sanitized_error(
					"payrexx_request",
					RuntimeError(exception_text),
					http_status=503,
				)
		finally:
			frappe.local.form_dict = original_form_dict

		core_log_error.assert_not_called()
		get_traceback.assert_not_called()
		capture_exception.assert_not_called()
		after = set(frappe.get_all("Error Log", filters={"fingerprint": fingerprint}, pluck="name"))
		created = after - before
		for created_name in created:
			self.addCleanup(frappe.db.delete, "Error Log", {"name": created_name})
		self.assertEqual(len(created), 1)
		name = created.pop()
		row = frappe.db.get_value(
			"Error Log", name, ["method", "error", "metadata", "fingerprint"], as_dict=True
		)
		self.assertEqual(row.error, message)
		self.assertEqual(row.method, "Payrexx request failed")
		self.assertEqual(row.metadata, "{}")
		self.assertEqual(row.fingerprint, fingerprint)
		persisted = frappe.as_json(row)
		for sensitive_value in (
			exception_text,
			provider_url,
			provider_response,
			payer_email,
			api_secret,
			"provider-private",
		):
			self.assertNotIn(sensitive_value, persisted)

	def test_retryable_database_errors_propagate_without_logging(self) -> None:
		for error_type in (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
			error = error_type("retry complete transaction")
			with (
				self.subTest(error_type=error_type.__name__),
				patch.object(frappe, "get_doc") as get_doc,
				patch.object(error_logging, "_log_to_file") as file_log,
				self.assertRaises(error_type) as raised,
			):
				error_logging.log_sanitized_error("payrexx_request", error)

			self.assertIs(raised.exception, error)
			get_doc.assert_not_called()
			file_log.assert_not_called()
