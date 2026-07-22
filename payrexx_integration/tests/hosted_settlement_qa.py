"""Protected CLI for hosted Payrexx sandbox settlement acceptance."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import requests

PREFLIGHT_METHOD = "payrexx_integration.hosted_qa.preflight"
SETTLEMENT_METHOD = "payrexx_integration.hosted_qa.inspect_settlement"


def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--mode", choices=("preflight", "settlement"), required=True)
	args = parser.parse_args()

	base_url = _validated_base_url(
		os.environ["PAYREXX_HOSTED_QA_BASE_URL"],
		os.environ["PAYREXX_HOSTED_QA_ALLOWED_HOSTS"],
	)
	run_id = os.environ["PAYREXX_HOSTED_QA_RUN_ID"]
	state_path = Path(
		os.environ.get(
			"PAYREXX_HOSTED_QA_STATE",
			f"/tmp/opencode/payrexx-hosted/{run_id}/state.json",
		)
	)
	session = _login(base_url)

	if args.mode == "preflight":
		result = _call(session, base_url, PREFLIGHT_METHOD, {"run_id": run_id})
		_write_state(state_path, result)
		print(json.dumps(result, indent=2, sort_keys=True))
		return

	state = _read_state(state_path)
	payment_request, integration_request = _state_record_names(state, run_id)
	result = _call(
		session,
		base_url,
		SETTLEMENT_METHOD,
		{
			"run_id": run_id,
			"payment_request_name": payment_request,
			"integration_request_name": integration_request,
		},
	)
	_write_state(state_path, state | {"settlement": result})
	print(json.dumps(result, indent=2, sort_keys=True))
	if not result.get("settled"):
		failed_checks = sorted(name for name, passed in result.get("checks", {}).items() if not passed)
		raise RuntimeError("Settlement is incomplete: " + ", ".join(failed_checks))


def _validated_base_url(value: str, allowed_hosts: str) -> str:
	parsed = urlparse(value)
	allowed = {host.strip().lower() for host in allowed_hosts.split(",") if host.strip()}
	if (
		parsed.scheme != "https"
		or not parsed.hostname
		or parsed.username
		or parsed.password
		or parsed.port not in (None, 443)
		or parsed.path not in ("", "/")
		or parsed.query
		or parsed.fragment
		or parsed.hostname.lower() not in allowed
	):
		raise ValueError("Hosted QA base URL must be an exact allowlisted HTTPS origin")
	return f"https://{parsed.hostname}"


def _login(base_url: str) -> requests.Session:
	session = requests.Session()
	response = session.post(
		f"{base_url}/api/method/login",
		data={
			"usr": os.environ["PAYREXX_HOSTED_QA_USER"],
			"pwd": os.environ["PAYREXX_HOSTED_QA_PASSWORD"],
		},
		timeout=30,
	)
	response.raise_for_status()
	if response.json().get("message") != "Logged In":
		raise RuntimeError("Hosted QA login failed")
	return session


def _call(session: requests.Session, base_url: str, method: str, data: dict) -> dict:
	response = session.post(f"{base_url}/api/method/{method}", data=data, timeout=60)
	response.raise_for_status()
	result = response.json().get("message")
	if not isinstance(result, dict):
		raise RuntimeError(f"Hosted QA method {method} returned an invalid response")
	return result


def _write_state(path: Path, state: dict) -> None:
	serialized = json.dumps(state, indent=2, sort_keys=True) + "\n"
	for forbidden in ("token", "password", "secret", "payment_url", "checkout_url"):
		if forbidden in serialized.lower():
			raise RuntimeError(f"Refusing to persist sensitive field containing {forbidden!r}")
	path.parent.mkdir(parents=True, exist_ok=True)
	flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
	file_descriptor = os.open(path, flags, 0o600)
	with os.fdopen(file_descriptor, "w", encoding="utf-8") as state_file:
		state_file.write(serialized)


def _read_state(path: Path) -> dict:
	if not path.is_file():
		raise RuntimeError(f"Hosted QA state does not exist: {path}")
	return json.loads(path.read_text(encoding="utf-8"))


def _state_record_names(state: dict, run_id: str) -> tuple[str, str]:
	if state.get("run_id") != run_id:
		raise RuntimeError("Hosted QA state belongs to a different run marker")
	payment_request = state.get("payment_request")
	integration_request = state.get("integration_request")
	if not payment_request or not integration_request:
		raise RuntimeError("Run preflight again after opening the normal invoice payment link")
	return payment_request, integration_request


if __name__ == "__main__":
	main()
