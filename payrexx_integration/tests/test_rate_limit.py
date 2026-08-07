# Copyright (c) 2026, Goodvantage GmbH and contributors

"""Payrexx edge rate limiting (600 requests / 5 minutes, AWS WAF).

The limit surfaces as 405 and then 403 — neither of which reads like a rate
limit — so an idempotent call backs off instead of hammering, and a POST never
replays at all.
"""

from unittest.mock import Mock, patch

from frappe.tests import UnitTestCase
from requests import HTTPError

from payrexx_integration.payrexx_integration.payrexx.payrexx_client import (
	RATE_LIMIT_STATUSES,
	PayrexxClient,
)

_CLIENT_MODULE = "payrexx_integration.payrexx_integration.payrexx.payrexx_client"


def _http_error(status_code: int) -> HTTPError:
	response = Mock()
	response.status_code = status_code
	return HTTPError(response=response)


class TestRateLimitBackoff(UnitTestCase):
	def _client(self) -> PayrexxClient:
		return PayrexxClient(instance="demo", api_secret="sk_test_dummy")

	def test_rate_limited_get_backs_off_and_succeeds(self):
		client = self._client()
		success = {"status": "success", "data": [{"id": 7}]}
		with (
			patch(
				f"{_CLIENT_MODULE}._execute_request",
				side_effect=[_http_error(405), _http_error(429), success],
			) as execute,
			patch(f"{_CLIENT_MODULE}.time.sleep") as sleep,
		):
			self.assertEqual(client.retrieve_gateway(7), {"id": 7})

		self.assertEqual(execute.call_count, 3)
		# Backoff grows, and is never a busy loop.
		self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.5])

	def test_intermediate_attempts_do_not_log_but_the_last_one_does(self):
		"""A call that recovers must not leave one Error Log row per attempt."""
		client = self._client()
		success = {"status": "success", "data": [{"id": 7}]}
		with (
			patch(
				f"{_CLIENT_MODULE}._execute_request",
				side_effect=[_http_error(405), success],
			) as execute,
			patch(f"{_CLIENT_MODULE}.time.sleep"),
		):
			client.retrieve_gateway(7)

		tolerated = [call.kwargs.get("expected_statuses") for call in execute.call_args_list]
		self.assertEqual(tolerated[0], RATE_LIMIT_STATUSES)
		self.assertEqual(tolerated[-1], RATE_LIMIT_STATUSES)

	def test_persistent_rate_limit_surfaces_on_the_final_attempt(self):
		client = self._client()
		with (
			patch(
				f"{_CLIENT_MODULE}._execute_request",
				side_effect=[_http_error(405), _http_error(405), _http_error(405)],
			) as execute,
			patch(f"{_CLIENT_MODULE}.time.sleep"),
			self.assertRaises(HTTPError),
		):
			client.retrieve_gateway(7)

		self.assertEqual(execute.call_count, 3)
		# The last attempt tolerates nothing, so staff still get the Error Log.
		self.assertEqual(execute.call_args_list[-1].kwargs.get("expected_statuses"), ())

	def test_other_failures_are_not_retried(self):
		client = self._client()
		with (
			patch(f"{_CLIENT_MODULE}._execute_request", side_effect=_http_error(500)) as execute,
			patch(f"{_CLIENT_MODULE}.time.sleep") as sleep,
			self.assertRaises(HTTPError),
		):
			client.retrieve_gateway(7)

		self.assertEqual(execute.call_count, 1)
		sleep.assert_not_called()

	def test_post_is_never_replayed(self):
		"""A create rejected at the edge probably never landed — probably is not enough."""
		client = self._client()
		with (
			patch(f"{_CLIENT_MODULE}._execute_request", side_effect=_http_error(405)) as execute,
			patch(f"{_CLIENT_MODULE}.time.sleep") as sleep,
			self.assertRaises(HTTPError),
		):
			client.create_gateway({"amount": 1000, "currency": "CHF"})

		self.assertEqual(execute.call_count, 1)
		sleep.assert_not_called()
