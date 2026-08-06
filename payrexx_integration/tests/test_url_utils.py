from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from payrexx_integration.url_utils import safe_return_url


class TestSafeReturnUrl(UnitTestCase):
	def test_same_host_absolute_url_passes(self):
		with patch.dict(frappe.conf, {"host_name": "https://npo.example.org"}, clear=True):
			self.assertEqual(
				safe_return_url("https://npo.example.org/spenden?donation_status=success"),
				"https://npo.example.org/spenden?donation_status=success",
			)

	def test_normalized_same_origin_passes(self):
		with patch.dict(frappe.conf, {"host_name": "https://npo.example.org"}, clear=True):
			self.assertEqual(
				safe_return_url("https://NPO.EXAMPLE.ORG:443/spenden"),
				"https://NPO.EXAMPLE.ORG:443/spenden",
			)

	def test_configured_public_base_url_passes(self):
		conf = {
			"host_name": "https://npo.example.org",
			"good_npo_public_base_url": "https://npo.tunnel.example.org",
		}
		with patch.dict(frappe.conf, conf, clear=True):
			self.assertEqual(
				safe_return_url("https://npo.tunnel.example.org/spenden"),
				"https://npo.tunnel.example.org/spenden",
			)

	def test_explicit_http_public_origin_passes_only_for_http(self):
		conf = {
			"host_name": "https://npo.example.org",
			"good_npo_public_base_url": "http://localhost:8000",
		}
		with patch.dict(frappe.conf, conf, clear=True):
			self.assertEqual(
				safe_return_url("http://localhost:8000/spenden"),
				"http://localhost:8000/spenden",
			)
			with self.assertRaises(frappe.PermissionError):
				safe_return_url("https://localhost:8000/spenden")

	def test_foreign_host_is_rejected(self):
		conf = {
			"host_name": "https://npo.example.org",
			"good_npo_public_base_url": "https://npo.tunnel.example.org",
		}
		with patch.dict(frappe.conf, conf, clear=True):
			with self.assertRaises(frappe.PermissionError):
				safe_return_url("https://evil.example.org/phish")

	def test_scheme_downgrade_is_rejected(self):
		conf = {
			"host_name": "https://npo.example.org",
			"good_npo_public_base_url": "https://npo.tunnel.example.org",
		}
		with patch.dict(frappe.conf, conf, clear=True):
			for target in (
				"http://npo.example.org/spenden",
				"http://npo.tunnel.example.org/spenden",
			):
				with self.subTest(target=target), self.assertRaises(frappe.PermissionError):
					safe_return_url(target)

	def test_different_effective_port_is_rejected(self):
		with patch.dict(frappe.conf, {"host_name": "https://npo.example.org"}, clear=True):
			with self.assertRaises(frappe.PermissionError):
				safe_return_url("https://npo.example.org:444/spenden")

	def test_userinfo_is_rejected(self):
		with patch.dict(frappe.conf, {"host_name": "https://npo.example.org"}, clear=True):
			with self.assertRaises(frappe.PermissionError):
				safe_return_url("https://user:password@npo.example.org/spenden")

	def test_configured_origin_with_userinfo_is_not_trusted(self):
		conf = {
			"host_name": "https://npo.example.org",
			"good_npo_public_base_url": "https://user:password@npo.tunnel.example.org",
		}
		with patch.dict(frappe.conf, conf, clear=True):
			with self.assertRaises(frappe.PermissionError):
				safe_return_url("https://npo.tunnel.example.org/spenden")

	def test_malformed_port_is_rejected(self):
		with patch.dict(frappe.conf, {"host_name": "https://npo.example.org"}, clear=True):
			with self.assertRaises(frappe.PermissionError):
				safe_return_url("https://npo.example.org:not-a-port/spenden")

	def test_malformed_configured_origins_are_not_trusted(self):
		malformed_origins = (
			"npo.tunnel.example.org",
			"https://npo.tunnel.example.org:not-a-port",
			"https://npo.tunnel.example.org:",
			"https://npo.tunnel.example.org:0",
			"https://[not-an-ipv6-address",
			"https://npo.tunnel.example.org/invalid path",
		)
		for configured_origin in malformed_origins:
			conf = {
				"host_name": "https://npo.example.org",
				"good_npo_public_base_url": configured_origin,
			}
			with self.subTest(configured_origin=configured_origin), patch.dict(frappe.conf, conf, clear=True):
				with self.assertRaises(frappe.PermissionError):
					safe_return_url("https://npo.tunnel.example.org/spenden")

	def test_scheme_relative_urls_are_rejected(self):
		with patch.dict(frappe.conf, {"host_name": "https://npo.example.org"}, clear=True):
			for target in (
				"//npo.example.org/spenden",
				"\\\\npo.example.org/spenden",
				"/\\npo.example.org/spenden",
				"\\/npo.example.org/spenden",
			):
				with self.subTest(target=target), self.assertRaises(frappe.PermissionError):
					safe_return_url(target)

	def test_non_http_scheme_is_rejected(self):
		with patch.dict(frappe.conf, {"host_name": "https://npo.example.org"}, clear=True):
			with self.assertRaises(frappe.PermissionError):
				safe_return_url("javascript:alert(1)")

	def test_relative_path_is_expanded_to_public_url(self):
		with patch.dict(frappe.conf, {"host_name": "https://npo.example.org"}, clear=True):
			self.assertEqual(
				safe_return_url("/payment-success"),
				"https://npo.example.org/payment-success",
			)
