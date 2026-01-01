"""Unit tests for broker app.py key validation"""

import dataclasses
import os
import re
import tempfile
import unittest
from typing import Optional
from unittest import mock

import yaml

from wgkex.allowlist.allowlist import AllowlistManager

# Copy necessary code from app.py for testing
WG_PUBKEY_PATTERN = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$")


def is_valid_wg_pubkey(pubkey: str) -> str:
    """Verifies if key is a valid WireGuard public key or not."""
    if WG_PUBKEY_PATTERN.match(pubkey) is None:
        raise ValueError(f"Not a valid Wireguard public key: {pubkey}.")
    return pubkey


def mock_is_valid_domain(domain: str) -> bool:
    """Mock for domain validation."""
    return True


@dataclasses.dataclass
class KeyExchange:
    """A key exchange message for testing."""

    public_key: str
    domain: str

    @classmethod
    def from_dict(
        cls, msg: dict, allowlist_mgr: Optional[AllowlistManager] = None
    ) -> "KeyExchange":
        """Creates a new KeyExchange message from dict."""
        public_key = is_valid_wg_pubkey(msg.get("public_key"))
        domain = str(msg.get("domain"))
        if not mock_is_valid_domain(domain):
            raise ValueError(f"Domain {domain} not in configured domains.")

        # Check allowlist if enabled
        if allowlist_mgr is not None:
            if not allowlist_mgr.is_key_allowed(domain, public_key):
                raise ValueError(f"Public key not in allowlist for domain {domain}.")

        return cls(public_key=public_key, domain=domain)


class KeyExchangeTest(unittest.TestCase):
    """Tests for KeyExchange class."""

    def test_from_dict_success_no_allowlist(self):
        """Test creating KeyExchange without allowlist."""
        msg = {
            "public_key": "o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg=",
            "domain": "ffmuc_welt",
        }

        key_exchange = KeyExchange.from_dict(msg)

        self.assertEqual(key_exchange.public_key, msg["public_key"])
        self.assertEqual(key_exchange.domain, msg["domain"])

    def test_from_dict_invalid_key_format(self):
        """Test KeyExchange rejects invalid key format."""
        msg = {
            "public_key": "not_a_valid_key",
            "domain": "ffmuc_welt",
        }

        with self.assertRaises(ValueError) as ctx:
            KeyExchange.from_dict(msg)
        self.assertIn("Not a valid Wireguard public key", str(ctx.exception))

    def test_from_dict_with_allowlist_key_allowed(self):
        """Test KeyExchange accepts key in allowlist."""
        test_dir = tempfile.mkdtemp()
        test_file = os.path.join(test_dir, "allowlist.yaml")

        try:
            # Create allowlist file
            allowlist_data = {
                "ffmuc_welt": ["o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg="]
            }
            with open(test_file, "w") as f:
                yaml.dump(allowlist_data, f)

            allowlist_mgr = AllowlistManager(test_file, refresh_interval=0)

            msg = {
                "public_key": "o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg=",
                "domain": "ffmuc_welt",
            }

            key_exchange = KeyExchange.from_dict(msg, allowlist_mgr)

            self.assertEqual(key_exchange.public_key, msg["public_key"])
            self.assertEqual(key_exchange.domain, msg["domain"])
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)
            os.rmdir(test_dir)

    def test_from_dict_with_allowlist_key_not_allowed(self):
        """Test KeyExchange rejects key not in allowlist."""
        test_dir = tempfile.mkdtemp()
        test_file = os.path.join(test_dir, "allowlist.yaml")

        try:
            # Create allowlist file with different key
            allowlist_data = {"ffmuc_welt": ["different_key_here_base64_format____="]}
            with open(test_file, "w") as f:
                yaml.dump(allowlist_data, f)

            allowlist_mgr = AllowlistManager(test_file, refresh_interval=0)

            msg = {
                "public_key": "o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg=",
                "domain": "ffmuc_welt",
            }

            with self.assertRaises(ValueError) as ctx:
                KeyExchange.from_dict(msg, allowlist_mgr)
            self.assertIn("not in allowlist", str(ctx.exception))
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)
            os.rmdir(test_dir)

    def test_from_dict_with_allowlist_domain_not_in_allowlist(self):
        """Test KeyExchange rejects domain not in allowlist."""
        test_dir = tempfile.mkdtemp()
        test_file = os.path.join(test_dir, "allowlist.yaml")

        try:
            # Create allowlist file with different domain
            allowlist_data = {
                "other_domain": ["o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg="]
            }
            with open(test_file, "w") as f:
                yaml.dump(allowlist_data, f)

            allowlist_mgr = AllowlistManager(test_file, refresh_interval=0)

            msg = {
                "public_key": "o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg=",
                "domain": "ffmuc_welt",
            }

            with self.assertRaises(ValueError) as ctx:
                KeyExchange.from_dict(msg, allowlist_mgr)
            self.assertIn("not in allowlist", str(ctx.exception))
        finally:
            if os.path.exists(test_file):
                os.remove(test_file)
            os.rmdir(test_dir)


class IsValidWgPubkeyTest(unittest.TestCase):
    """Tests for is_valid_wg_pubkey function."""

    def test_valid_key(self):
        """Test valid WireGuard public key."""
        valid_key = "o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg="
        result = is_valid_wg_pubkey(valid_key)
        self.assertEqual(result, valid_key)

    def test_invalid_key_too_short(self):
        """Test invalid key - too short."""
        with self.assertRaises(ValueError):
            is_valid_wg_pubkey("short")

    def test_invalid_key_wrong_format(self):
        """Test invalid key - wrong format."""
        with self.assertRaises(ValueError):
            is_valid_wg_pubkey("not_a_valid_base64_key_format!!!!!!!!!=")

    def test_invalid_key_no_equals(self):
        """Test invalid key - missing equals sign."""
        with self.assertRaises(ValueError):
            is_valid_wg_pubkey("o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg")


if __name__ == "__main__":
    unittest.main()
