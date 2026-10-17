"""Tests for allowlist.py"""

import os
import tempfile
import unittest
from unittest import mock

from wgkex.config import config
from wgkex.worker import allowlist


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


if __name__ == "__main__":
    unittest.main()
