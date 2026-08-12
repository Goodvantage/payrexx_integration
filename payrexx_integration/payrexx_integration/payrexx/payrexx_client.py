# Copyright (c) 2026, Goodvantage GmbH and contributors

import ipaddress
import re
import time
from collections.abc import Callable, Collection
from urllib.parse import parse_qs, urlencode, urlsplit

import frappe
import requests
from frappe.utils import get_request_session
from requests import HTTPError

from payrexx_integration.error_logging import TRANSACTION_ERRORS, log_sanitized_error

DEFAULT_API_BASE_DOMAIN = "payrexx.com"
DEFAULT_API_VERSION = "v1.16"
ALLOWED_API_HOSTS_CONFIG = "payrexx_allowed_api_hosts"
CREDENTIAL_PROBE_SENTINEL = {"status": "error", "message": "No Gateway found with id 0"}
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")

# Payrexx enforces 600 requests / 5 minutes at the CDN edge (AWS WAF). It
# answers 405 first and only starts answering 403 once the limit is well
# exceeded, so neither status reads like a rate limit and an immediate retry
# deepens it. 403 is deliberately NOT treated as a rate limit here: it is the
# same status Payrexx returns for a rejected API secret, and the custom-domain
# fallback already depends on that meaning.
RATE_LIMIT_STATUSES = frozenset({405, 429})
_RATE_LIMIT_RETRY_DELAYS = (0.5, 1.5)


class PayrexxAPIError(RuntimeError):
	pass


class PayrexxClient:
	"""Thin wrapper over the Payrexx v1.x REST API.

	Auth: API secret is sent in the 'x-api-key' header (current Payrexx scheme,
	per the official PHP SDK in payrexx/payrexx-php). The legacy ApiSignature
	body field is no longer required. Platform accounts can pass a custom
	``api_base_domain`` such as ``pay.goodvantage.ch``.

	The secret is kept **only** inside the closure of the requests auth callable
	built in ``__init__``. It is never stored as an attribute, built into a caller
	header dict, or passed into the send function as an ordinary argument. Failed
	requests cross the app-local context-free Error Log boundary; they never call
	``frappe.log_error`` or send a live exception/stack to Sentry.
	"""

	def __init__(
		self,
		instance: str,
		api_secret: str,
		api_version: str = DEFAULT_API_VERSION,
		api_base_domain: str | None = None,
	):
		if not instance:
			raise ValueError("Payrexx instance name is required")
		# Validate the destination before retaining or using the API secret. Settings
		# callers perform the same validation before reading the Password field.
		self.api_base_domain = _normalize_api_base_domain(api_base_domain)
		if not api_secret:
			raise ValueError("Payrexx API secret is required")
		self.instance = instance
		self._authorize = _api_key_auth(api_secret)
		self.version = api_version

	# ----------------------------------------------------------------- Gateway

	def create_gateway(self, params: dict) -> dict:
		"""POST /Gateway/  ->  dict with id, link, hash, status, ..."""
		return _unwrap(
			self._post(
				"Gateway/",
				data=params,
				retry_not_found=True,
			)
		)

	def retrieve_gateway(self, gateway_id: int) -> dict:
		"""GET /Gateway/{id}/"""
		return _unwrap(self._get(f"Gateway/{gateway_id}/"))

	def ping_gateway(self) -> dict:
		"""Accept only Payrexx's exact HTTP-200 Gateway-zero credential sentinel."""
		body = self._get("Gateway/0/")
		if body != CREDENTIAL_PROBE_SENTINEL:
			raise _logged_response_error("Unexpected credential probe response")
		return body

	# ----------------------------------------------------------------- static QR codes

	def create_qr_code(self, webshop_url: str) -> dict:
		"""POST /QrCode/  ->  dict with uuid, webshopUrl, png, svg (base64 data URIs).

		The returned code is a permanent static QR. A plain camera scan opens
		``webshop_url`` unchanged; a TWINT-app scan opens it with
		``qr_code_session_id`` plus ``returnAppScheme`` (iOS) or
		``returnAppPackage`` (Android) appended as query parameters.
		"""
		return _unwrap(self._post("QrCode/", data={"webshopUrl": webshop_url}))

	def delete_qr_code(self, qr_code_uuid: str, *, expected_statuses: Collection[int] = ()) -> None:
		"""DELETE /QrCode/{uuid}/

		``expected_statuses`` names provider HTTP statuses the caller already
		handles as a normal outcome (``delete_static_qr`` treats 404 as "already
		deleted"). They are still raised — only the Error Log row is skipped, so
		a tolerated outcome does not surface to staff as a failure.
		"""
		resp = self._delete(f"QrCode/{qr_code_uuid}/", expected_statuses=expected_statuses)
		if isinstance(resp, dict) and resp.get("status") not in (None, "success"):
			raise _logged_response_error(resp.get("message", "Unknown Payrexx error"))

	# ------------------------------------------------------------ Subscription

	def create_subscription(self, params: dict) -> dict:
		"""POST /Subscription/

		Rarely usable in practice: Payrexx requires ``userId``, the id of a
		contact that only exists once the payer has transacted. Subscriptions are
		normally created by sending the payer through a Gateway built with
		``subscriptionState``; this exists for the case where the contact is
		already known.
		"""
		return _unwrap(self._post("Subscription/", data=params))

	def retrieve_subscription(self, subscription_id: int) -> dict:
		"""GET /Subscription/{id}/"""
		return _unwrap(self._get(f"Subscription/{subscription_id}/"))

	def list_subscriptions(self, *, offset: int | None = None, limit: int | None = None) -> list[dict]:
		"""GET /Subscription/ — every subscription on the instance, newest ordering left to Payrexx."""
		pagination = {}
		if offset is not None:
			pagination["offset"] = int(offset)
		if limit is not None:
			pagination["limit"] = int(limit)
		return _unwrap_list(self._get("Subscription/", query=pagination or None))

	def list_transactions(
		self,
		*,
		datetime_utc_greater_than: str,
		datetime_utc_less_than: str,
		my_transactions_only: bool = True,
		order_by_time: str = "ASC",
		offset: int | None = None,
		limit: int | None = None,
	) -> list[dict]:
		"""GET /Transaction/ using the official SDK's bounded query filters."""
		query = {
			"filterDatetimeUtcGreaterThan": datetime_utc_greater_than,
			"filterDatetimeUtcLessThan": datetime_utc_less_than,
			"filterMyTransactionsOnly": int(bool(my_transactions_only)),
			"orderByTime": order_by_time,
		}
		if offset is not None:
			query["offset"] = int(offset)
		if limit is not None:
			query["limit"] = int(limit)
		return _unwrap_list(self._get("Transaction/", query=query))

	def update_subscription(self, subscription_id: int, params: dict) -> dict:
		"""PUT /Subscription/{id}/ — amount/currency/purpose/vatRate.

		Payrexx applies a new amount from the next payment interval, not to the
		charge already taken.
		"""
		return _unwrap(self._put(f"Subscription/{subscription_id}/", data=params))

	def cancel_subscription(self, subscription_id: int, *, expected_statuses: Collection[int] = ()) -> dict:
		"""DELETE /Subscription/{id}/ — immediate, and the only cancellation the API offers.

		Absent from Payrexx's published API reference but exercised by their own
		PHP SDK's examples. End-of-period cancellation (the ``in_notice`` state)
		exists only in the merchant admin.
		"""
		try:
			response = self._delete(f"Subscription/{subscription_id}/", expected_statuses=expected_statuses)
		except HTTPError as exc:
			if get_http_status(exc) == 404 and 404 in expected_statuses:
				return {"status": "success", "already_gone": True}
			raise
		if response is None:
			return {"status": "success"}
		if not isinstance(response, dict) or response.get("status") != "success":
			message = response.get("message", "Unknown Payrexx error") if isinstance(response, dict) else None
			raise _logged_response_error(message or "Unknown Payrexx error")
		return response

	# ----------------------------------------------------------------- internal

	def _get(
		self,
		path: str,
		*,
		retry_not_found: bool = False,
		query: dict | None = None,
		json_data: dict | None = None,
	) -> dict:
		return self._send_idempotent(
			lambda tolerated: self._get_once(
				path,
				retry_not_found=retry_not_found,
				query=query,
				json_data=json_data,
				expected_statuses=tolerated,
			)
		)

	def _get_once(
		self,
		path: str,
		*,
		retry_not_found: bool,
		query: dict | None = None,
		json_data: dict | None = None,
		expected_statuses: Collection[int] = (),
	) -> dict:
		try:
			return _execute_request(
				"GET",
				self._url(path, query),
				authorize=self._authorize,
				json_data=json_data,
				expected_statuses=self._initial_expected_statuses(
					expected_statuses, retry_not_found=retry_not_found
				),
			)
		except Exception as exc:
			if self._should_retry_default_domain(exc, retry_not_found=retry_not_found):
				fallback_url = self._url(path, query, api_base_domain=DEFAULT_API_BASE_DOMAIN)
				return _execute_request(
					"GET",
					fallback_url,
					authorize=self._authorize,
					json_data=json_data,
					expected_statuses=expected_statuses,
				)
			raise

	def _put(self, path: str, *, data: dict) -> dict:
		# PUT is idempotent by definition here — setting an amount twice leaves
		# the same amount — so unlike POST it is safe to replay under a rate limit.
		return self._send_idempotent(
			lambda tolerated: self._put_once(path, data=data, expected_statuses=tolerated)
		)

	def _put_once(self, path: str, *, data: dict, expected_statuses: Collection[int] = ()) -> dict:
		try:
			return _execute_request(
				"PUT",
				self._url(path),
				authorize=self._authorize,
				data=data,
				expected_statuses=self._initial_expected_statuses(expected_statuses, retry_not_found=False),
			)
		except Exception as exc:
			if self._should_retry_default_domain(exc, retry_not_found=False):
				fallback_url = self._url(path, api_base_domain=DEFAULT_API_BASE_DOMAIN)
				return _execute_request(
					"PUT",
					fallback_url,
					authorize=self._authorize,
					data=data,
					expected_statuses=expected_statuses,
				)
			raise

	def _post(self, path: str, *, data: dict, retry_not_found: bool = False) -> dict:
		# Deliberately not rate-limit-retried. A POST that was rejected at the
		# edge almost certainly never reached Payrexx, but "almost certainly" is
		# not a basis for replaying a call that creates a Gateway — the orphan
		# recovery path exists because an unknown create outcome is expensive.
		try:
			return _execute_request(
				"POST",
				self._url(path),
				authorize=self._authorize,
				data=data,
				expected_statuses=self._initial_expected_statuses((), retry_not_found=retry_not_found),
			)
		except Exception as exc:
			if self._should_retry_default_domain(exc, retry_not_found=retry_not_found):
				fallback_url = self._url(path, api_base_domain=DEFAULT_API_BASE_DOMAIN)
				return _execute_request("POST", fallback_url, authorize=self._authorize, data=data)
			raise

	def _delete(self, path: str, *, expected_statuses: Collection[int] = ()) -> dict:
		return self._send_idempotent(
			lambda tolerated: self._delete_once(path, expected_statuses=[*expected_statuses, *tolerated])
		)

	def _delete_once(self, path: str, *, expected_statuses: Collection[int] = ()) -> dict:
		try:
			return _execute_request(
				"DELETE",
				self._url(path),
				authorize=self._authorize,
				expected_statuses=self._initial_expected_statuses(expected_statuses, retry_not_found=False),
			)
		except Exception as exc:
			# A concrete resource DELETE 404 is authoritative (the code is already
			# gone), so it must not fall back to the canonical host — only 401/403
			# may retry there. Mirrors the concrete Gateway retrieval rule.
			if self._should_retry_default_domain(exc, retry_not_found=False):
				fallback_url = self._url(path, api_base_domain=DEFAULT_API_BASE_DOMAIN)
				return _execute_request(
					"DELETE",
					fallback_url,
					authorize=self._authorize,
					expected_statuses=expected_statuses,
				)
			raise

	def _send_idempotent(self, operation: Callable[[Collection[int]], dict]) -> dict:
		"""Run an idempotent request, backing off when Payrexx's edge rate-limits it.

		Only GET, PUT, and DELETE come through here: all are safe to repeat, and the
		reconciliation sweep that iterates subscriptions is exactly the caller
		that will meet the limit as donor count grows.

		Intermediate attempts declare the rate-limit statuses expected so a call
		that eventually succeeds does not leave an Error Log row per attempt. The
		final attempt tolerates nothing extra, so a persistent rate limit still
		surfaces to staff exactly as any other provider failure would.
		"""
		for delay in _RATE_LIMIT_RETRY_DELAYS:
			try:
				return operation(RATE_LIMIT_STATUSES)
			except Exception as exc:
				if get_http_status(exc) not in RATE_LIMIT_STATUSES:
					raise
				time.sleep(delay)
		return operation(())

	def _should_retry_default_domain(self, exc: Exception, *, retry_not_found: bool) -> bool:
		if self.api_base_domain == DEFAULT_API_BASE_DOMAIN:
			return False
		if not isinstance(exc, HTTPError):
			return False
		status_code = getattr(exc.response, "status_code", None)
		return status_code in {401, 403} or (retry_not_found and status_code == 404)

	def _initial_expected_statuses(
		self, expected_statuses: Collection[int], *, retry_not_found: bool
	) -> Collection[int]:
		"""Suppress an intermediate custom-host failure that will be retried."""
		if self.api_base_domain == DEFAULT_API_BASE_DOMAIN:
			return expected_statuses
		fallback_statuses = (401, 403, 404) if retry_not_found else (401, 403)
		return tuple(dict.fromkeys((*expected_statuses, *fallback_statuses)))

	def _url(
		self,
		path: str,
		query: dict | None = None,
		*,
		api_base_domain: str | None = None,
	) -> str:
		q = {"instance": self.instance}
		if query:
			q.update(query)
		domain = _normalize_api_base_domain(api_base_domain or self.api_base_domain)
		return f"https://api.{domain}/{self.version}/{path.lstrip('/')}?{urlencode(q)}"


def _api_key_auth(api_secret: str) -> Callable[[requests.PreparedRequest], requests.PreparedRequest]:
	"""Build a requests auth callable that holds the API secret in its closure.

	A closure cell keeps the secret outside ordinary frame variables and expanded
	client attributes even if a caller later adds unsafe exception inspection.
	"""

	def apply_api_key(prepared_request: requests.PreparedRequest) -> requests.PreparedRequest:
		prepared_request.headers["x-api-key"] = api_secret
		return prepared_request

	return apply_api_key


def _execute_request(
	method: str,
	url: str,
	*,
	authorize: Callable[[requests.PreparedRequest], requests.PreparedRequest],
	data: dict | None = None,
	json_data: dict | None = None,
	expected_statuses: Collection[int] = (),
) -> dict | list | str | None:
	"""Send one authenticated Payrexx request without leaking credentials or payer data.

	Deliberately does not use ``frappe.integrations.utils.make_*_request``: that
	helper takes the auth header and form body as ordinary arguments and reports
	failures through core traceback/Sentry capture. Response parsing mirrors
	``make_request``; error reporting intentionally does not.

	``expected_statuses`` lets a caller declare provider HTTP statuses it handles
	as a normal outcome; those are re-raised without an Error Log row. It is empty
	by default, so every existing call site keeps logging exactly as before.
	"""
	headers = {"Accept": "application/json"}
	if data is not None:
		headers["Content-Type"] = "application/x-www-form-urlencoded"

	session = get_request_session()
	prepared_request = session.prepare_request(
		requests.Request(method=method, url=url, headers=headers, data=data, json=json_data, auth=authorize)
	)
	# The body is now sealed inside the prepared request. Drop this frame's own
	# reference before provider I/O and any failure handling.
	data = None
	json_data = None
	# ``Session.request()`` merges proxy, CA-bundle, and client-certificate settings
	# from the environment before sending; ``Session.send()`` does not. Replicate it
	# so proxied and custom-CA deployments behave exactly as they did.
	environment_settings = session.merge_environment_settings(prepared_request.url, {}, None, None, None)

	try:
		# Bounded (connect, read) timeout: get_payment_url runs inline in the
		# guest checkout web request, so a hung provider connection must never
		# pin a worker (an unbounded wait exhausts the pool and downs the site).
		response = frappe.flags.integration_request = session.send(
			prepared_request, timeout=(5, 30), **environment_settings
		)
		response.raise_for_status()
		return _parse_response(response)
	except TRANSACTION_ERRORS:
		raise
	except Exception as exc:
		# Statuses the caller declared expected are a normal, handled outcome, not
		# staff-visible breakage: re-raise them so the caller still decides, but
		# without an Error Log row. The direct helper reads only its class and status;
		# it never serializes the exception or live frames.
		if get_http_status(exc) not in expected_statuses:
			log_sanitized_error("payrexx_request", exc, http_status=get_http_status(exc))
		raise


def _parse_response(response: requests.Response) -> dict | list | str | None:
	"""Mirror ``frappe.integrations.utils.make_request`` content-type handling."""
	if content_type := response.headers.get("content-type"):
		if content_type == "text/plain; charset=utf-8":
			return parse_qs(response.text)
		elif content_type.startswith("application/") and content_type.split(";")[0].endswith("json"):
			return response.json()
		elif response.text:
			return response.text
	return None


def _unwrap(resp: dict) -> dict:
	"""Payrexx wraps ok responses as {status: 'success', data: [obj]}."""
	return next(iter(_unwrap_list(resp)), {})


def _unwrap_list(resp: dict) -> list[dict]:
	"""The same envelope, for endpoints that legitimately return many rows."""
	if not isinstance(resp, dict) or resp.get("status") != "success":
		message = resp.get("message", "Unknown Payrexx error") if isinstance(resp, dict) else None
		raise _logged_response_error(message or "Unknown Payrexx error")
	return resp.get("data") or []


def _logged_response_error(message: str) -> PayrexxAPIError:
	exception = PayrexxAPIError(message)
	log_sanitized_error("payrexx_response", exception, http_status=200)
	return exception


_SUBSCRIPTION_INTERVAL = re.compile(r"^P(\d{1,3})([MY])$")


def validate_subscription_interval(value, label: str) -> str:
	"""Accept only the ISO-8601 durations this integration actually supports.

	Payrexx takes anything PHP's ``DateInterval`` parses, but these strings end
	up in a signed provider payload built partly from operator and caller input,
	so the accepted set is the one the product offers — monthly, quarterly,
	yearly — rather than everything the provider would tolerate.
	"""
	candidate = str(value or "").strip().upper()
	if not _SUBSCRIPTION_INTERVAL.fullmatch(candidate):
		raise ValueError(
			f"{label} must be an ISO-8601 duration in months or years, such as P1M, P3M or P1Y; got {value!r}"
		)
	return candidate


def _normalize_api_base_domain(value: str | None) -> str:
	host = _parse_bare_host(value or DEFAULT_API_BASE_DOMAIN, "Payrexx API Base Domain")
	if host.startswith("api."):
		host = host.removeprefix("api.")
	if not host or host.startswith("api."):
		raise ValueError("Payrexx API Base Domain is malformed")

	api_host = f"api.{host}"
	if _is_canonical_payrexx_host(api_host) or api_host in _configured_allowed_api_hosts():
		return host
	raise ValueError(
		f"Payrexx API host {api_host} is not trusted; add the exact host to "
		f"{ALLOWED_API_HOSTS_CONFIG} in site_config.json"
	)


def _parse_bare_host(value: str, label: str) -> str:
	if not isinstance(value, str) or not value or value != value.strip():
		raise ValueError(f"{label} must be a bare hostname")
	if _CONTROL_CHARACTERS.search(value):
		raise ValueError(f"{label} contains control characters")
	if "://" in value:
		raise ValueError(f"{label} must not include a URL scheme")

	try:
		parts = urlsplit(f"//{value}")
		port = parts.port
	except ValueError as exc:
		raise ValueError(f"{label} is malformed") from exc
	if parts.username is not None or parts.password is not None or "@" in parts.netloc:
		raise ValueError(f"{label} must not include user information")
	if parts.path or parts.query or parts.fragment:
		raise ValueError(f"{label} must not include a path, query, or fragment")
	if port not in (None, 443):
		raise ValueError(f"{label} must not include port {port}")

	host = (parts.hostname or "").lower()
	if not host or host.endswith(".") or len(host) > 253:
		raise ValueError(f"{label} is malformed")
	normalized_netloc = host if port is None else f"{host}:{port}"
	if parts.netloc.lower() != normalized_netloc:
		raise ValueError(f"{label} is malformed")
	try:
		host.encode("ascii")
	except UnicodeEncodeError as exc:
		raise ValueError(f"{label} must use an ASCII hostname") from exc
	try:
		ipaddress.ip_address(host)
	except ValueError:
		pass
	else:
		raise ValueError(f"{label} must not be an IP address")

	labels = host.split(".")
	if len(labels) < 2 or all(part.isdigit() for part in labels):
		raise ValueError(f"{label} is malformed")
	if any(not _HOST_LABEL.fullmatch(part) for part in labels):
		raise ValueError(f"{label} is malformed")
	return host


def _is_canonical_payrexx_host(host: str) -> bool:
	return host == "api.payrexx.com" or host.endswith(".payrexx.com")


def _configured_allowed_api_hosts() -> set[str]:
	configured = frappe.conf.get(ALLOWED_API_HOSTS_CONFIG)
	if configured in (None, ""):
		return set()
	if not isinstance(configured, list):
		raise ValueError(f"{ALLOWED_API_HOSTS_CONFIG} must be a JSON list of exact API hostnames")

	allowed_hosts = set()
	for value in configured:
		allowed_hosts.add(_parse_bare_host(value, ALLOWED_API_HOSTS_CONFIG))
	return allowed_hosts


def get_http_status(exc: Exception) -> int | None:
	response = getattr(exc, "response", None)
	return getattr(response, "status_code", None)
