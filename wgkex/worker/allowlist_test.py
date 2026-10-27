"""Tests for allowlist.py"""

import os
import tempfile
import unittest
from unittest import mock

from wgkex.config import config
from wgkex.worker import allowlist
from wgkex.worker.gateway_keys import gateway_keys


class AllowlistTest(unittest.TestCase):
    def setUp(self) -> None:
        allowlist.reset()
        self.addCleanup(allowlist.reset)

    @mock.patch.object(config, "get_config")
    def test_from_config_and_file(self, config_mock):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("FILEKEY= # comment\n")
        self.addCleanup(os.unlink, f.name)
        config_mock.return_value.key_lists = config.KeyLists(
            allowlist=["CFGKEY="], allowlist_file=f.name
        )
        self.assertTrue(allowlist.is_allowlisted("CFGKEY="))
        self.assertTrue(allowlist.is_allowlisted("FILEKEY="))
        self.assertFalse(allowlist.is_allowlisted("OTHER="))

    @mock.patch.object(config, "get_config")
    def test_unconfigured(self, config_mock):
        config_mock.return_value = mock.MagicMock()
        self.assertFalse(allowlist.is_allowlisted("ANY="))
        self.assertFalse(allowlist.get_allowlist().configured)

    @mock.patch.object(config, "get_config")
    def test_is_protected(self, config_mock):
        """Allowlisted keys and keys of gateways are protected."""
        config_mock.return_value.key_lists = config.KeyLists(allowlist=["CFGKEY="])
        self.addCleanup(gateway_keys.reset)
        gateway_keys.add("GWKEY=")
        self.assertTrue(allowlist.is_protected("CFGKEY="))
        self.assertTrue(allowlist.is_protected("GWKEY="))
        self.assertFalse(allowlist.is_protected("OTHER="))

    def test_rebalancer_protects_gateway_keys_by_default(self):
        import inspect

        from wgkex.worker import rebalance

        default = inspect.signature(rebalance.Rebalancer).parameters["protected"]
        self.assertIs(default.default, allowlist.is_protected)


if __name__ == "__main__":
    unittest.main()
