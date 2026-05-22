# Copyright (c) 2026, Goodvantage GmbH and contributors

from urllib.parse import urlencode, urlsplit

from frappe.integrations.utils import make_get_request, make_post_request
from requests import HTTPError

DEFAULT_API_BASE_DOMAIN = "payrexx.com"


class PayrexxAPIError(RuntimeError):
	pass


class PayrexxClient:
	"""Thin wrapper over the Payrexx v1.x REST API.

	Auth: API secret is sent in the 'x-api-key' header (current Payrexx scheme,
	per the official PHP SDK in payrexx/payrexx-php). The legacy ApiSignature
	body field is no longer required. Platform accounts can pass a custom
	``api_base_domain`` such as ``pay.goodvantage.ch``.
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
		if not api_secret:
			raise ValueError("Payrexx API secret is required")
		self.instance = instance
		self.api_secret = api_secret
		self.version = api_version
		self.api_base_domain = _normalize_api_base_domain(api_base_domain)

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

	def delete_gateway(self, gateway_id: int) -> dict:
		# DELETE not exposed by frappe.integrations.utils; use a plain request
		# only when needed. Stub here for completeness.
		raise NotImplementedError("Delete via Payrexx dashboard or extend with a DELETE call.")

	# ------------------------------------------------------------- Transaction

	def retrieve_transaction(self, transaction_id: int) -> dict:
		"""GET /Transaction/{id}/"""
		return _unwrap(self._get(f"Transaction/{transaction_id}/"))

	# ----------------------------------------------------------------- internal

	def _get(self, path: str) -> dict:
		try:
			return make_get_request(url=self._url(path), headers=self._headers())
		except Exception as exc:
			if self._should_retry_default_domain(exc):
				return make_get_request(
					url=self._url(path, api_base_domain=DEFAULT_API_BASE_DOMAIN),
					headers=self._headers(),
				)
			raise

	def _post(self, path: str, *, data: dict) -> dict:
		headers = {
			**self._headers(),
			"Content-Type": "application/x-www-form-urlencoded",
		}
		try:
			return make_post_request(url=self._url(path), data=data, headers=headers)
		except Exception as exc:
			if self._should_retry_default_domain(exc):
				return make_post_request(
					url=self._url(path, api_base_domain=DEFAULT_API_BASE_DOMAIN),
					data=data,
					headers=headers,
				)
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
		domain = api_base_domain or self.api_base_domain
		return f"https://api.{domain}/{self.version}/{path.lstrip('/')}?{urlencode(q)}"

	def _headers(self) -> dict:
		return {"x-api-key": self.api_secret, "Accept": "application/json"}


def _unwrap(resp: dict) -> dict:
	"""Payrexx wraps ok responses as {status: 'success', data: [obj]}."""
	if not resp or resp.get("status") != "success":
		raise PayrexxAPIError((resp or {}).get("message", "Unknown Payrexx error"))
	data = resp.get("data") or []
	return data[0] if data else {}


def _normalize_api_base_domain(value: str | None) -> str:
	raw = (value or DEFAULT_API_BASE_DOMAIN).strip().rstrip("/")
	if not raw:
		return DEFAULT_API_BASE_DOMAIN
	if "://" in raw:
		parts = urlsplit(raw)
		raw = parts.netloc or parts.path
	raw = raw.strip().strip("/")
	if raw.startswith("api."):
		raw = raw[4:]
	return raw or DEFAULT_API_BASE_DOMAIN
