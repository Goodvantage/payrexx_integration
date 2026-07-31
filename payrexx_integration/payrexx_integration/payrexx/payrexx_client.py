# Copyright (c) 2026, Goodvantage GmbH and contributors

import ipaddress
import re
from collections.abc import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import frappe
import requests
from frappe.utils import get_request_session
from requests import HTTPError

DEFAULT_API_BASE_DOMAIN = "payrexx.com"
ALLOWED_API_HOSTS_CONFIG = "payrexx_allowed_api_hosts"
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class PayrexxAPIError(RuntimeError):
	pass


class PayrexxClient:
	"""Thin wrapper over the Payrexx v1.x REST API.

	Auth: API secret is sent in the 'x-api-key' header (current Payrexx scheme,
	per the official PHP SDK in payrexx/payrexx-php). The legacy ApiSignature
	body field is no longer required. Platform accounts can pass a custom
	``api_base_domain`` such as ``pay.goodvantage.ch``.

	The secret is kept **only** inside the closure of the requests auth callable
	built in ``__init__``. It is never stored as an attribute, never built into a
	header dict, and never passed to another function as an argument, because a
	failing provider request is logged with the frame variables of every frame in
	the traceback (``frappe.log_error`` -> ``frappe.get_traceback(with_context=True)``,
	plus Sentry when telemetry is on). That dump also expands plain objects, so an
	``self.api_secret`` attribute would leak just like a ``{"x-api-key": ...}``
	local: frappe's sanitizer redacts only the exact keys password/passwd/secret/
	token/key/pwd and does not match ``x-api-key``.
	"""

	def __init__(
		self,
		instance: str,
		api_secret: str,
		api_version: str = "v1.14",
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
			)
		)

	def retrieve_gateway(self, gateway_id: int) -> dict:
		"""GET /Gateway/{id}/"""
		return _unwrap(self._get(f"Gateway/{gateway_id}/"))

	def ping_gateway(self) -> dict:
		"""GET /Gateway/0/ for a cheap credential check without creating checkout data."""
		return self._get("Gateway/0/")

	# ----------------------------------------------------------------- static QR codes

	def create_qr_code(self, webshop_url: str) -> dict:
		"""POST /QrCode/  ->  dict with uuid, webshopUrl, png, svg (base64 data URIs).

		The returned code is a permanent static QR. A plain camera scan opens
		``webshop_url`` unchanged; a TWINT-app scan opens it with
		``qr_code_session_id`` plus ``returnAppScheme`` (iOS) or
		``returnAppPackage`` (Android) appended as query parameters.
		"""
		return _unwrap(self._post("QrCode/", data={"webshopUrl": webshop_url}))

	def delete_qr_code(self, qr_code_uuid: str) -> None:
		"""DELETE /QrCode/{uuid}/"""
		resp = self._delete(f"QrCode/{qr_code_uuid}/")
		if isinstance(resp, dict) and resp.get("status") not in (None, "success"):
			raise PayrexxAPIError(resp.get("message", "Unknown Payrexx error"))

	# ----------------------------------------------------------------- internal

	def _get(self, path: str) -> dict:
		try:
			return _execute_request("GET", self._url(path), authorize=self._authorize)
		except Exception as exc:
			if self._should_retry_default_domain(exc):
				fallback_url = self._url(path, api_base_domain=DEFAULT_API_BASE_DOMAIN)
				return _execute_request("GET", fallback_url, authorize=self._authorize)
			raise

	def _post(self, path: str, *, data: dict) -> dict:
		try:
			return _execute_request("POST", self._url(path), authorize=self._authorize, data=data)
		except Exception as exc:
			if self._should_retry_default_domain(exc):
				fallback_url = self._url(path, api_base_domain=DEFAULT_API_BASE_DOMAIN)
				return _execute_request("POST", fallback_url, authorize=self._authorize, data=data)
			raise

	def _delete(self, path: str) -> dict:
		try:
			return _execute_request("DELETE", self._url(path), authorize=self._authorize)
		except Exception as exc:
			if self._should_retry_default_domain(exc):
				fallback_url = self._url(path, api_base_domain=DEFAULT_API_BASE_DOMAIN)
				return _execute_request("DELETE", fallback_url, authorize=self._authorize)
			raise

	def _should_retry_default_domain(self, exc: Exception) -> bool:
		if self.api_base_domain == DEFAULT_API_BASE_DOMAIN:
			return False
		if not isinstance(exc, HTTPError):
			return False
		status_code = getattr(exc.response, "status_code", None)
		return status_code in {401, 403, 404}

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

	A closure cell is the one place the secret can live without appearing in the
	frame variables (or expanded object attributes) that frappe writes to Error
	Log — and to Sentry — for every failed provider request.
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
) -> dict | list | str | None:
	"""Send one authenticated Payrexx request without leaking credentials or payer data.

	Deliberately does not use ``frappe.integrations.utils.make_*_request``: that
	helper takes the auth header and the form body as ordinary arguments, so both
	become frame variables of framework code and are written verbatim into the
	Error Log traceback it produces on failure. Response handling and the error
	reporting contract are otherwise identical to ``make_request``.
	"""
	headers = {"Accept": "application/json"}
	if data is not None:
		headers["Content-Type"] = "application/x-www-form-urlencoded"

	session = get_request_session()
	prepared_request = session.prepare_request(
		requests.Request(method=method, url=url, headers=headers, data=data, auth=authorize)
	)
	# The body is now sealed inside the prepared request, which does not expose it
	# to traceback frame dumps. Drop this frame's own reference to the payer data.
	data = None
	# ``Session.request()`` merges proxy, CA-bundle, and client-certificate settings
	# from the environment before sending; ``Session.send()`` does not. Replicate it
	# so proxied and custom-CA deployments behave exactly as they did.
	environment_settings = session.merge_environment_settings(prepared_request.url, {}, None, None, None)

	try:
		response = frappe.flags.integration_request = session.send(prepared_request, **environment_settings)
		response.raise_for_status()
		return _parse_response(response)
	except Exception:
		if frappe.flags.integration_request_doc:
			frappe.flags.integration_request_doc.log_error()
		else:
			frappe.log_error()
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
	if not resp or resp.get("status") != "success":
		raise PayrexxAPIError((resp or {}).get("message", "Unknown Payrexx error"))
	data = resp.get("data") or []
	return data[0] if data else {}


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
