"""Tests for configuration handling class."""

import unittest

import mock
import yaml

from wgkex.config import config

_VALID_CFG = (
    "domain_prefixes:\n- ffmuc_\n- ffdon_\n- ffwert_\nlog_level: DEBUG\ndomains:\n- a\n- b\nmqtt:\n  broker_port: 1883"
    "\n  broker_url: mqtt://broker\n  keepalive: 5\n  password: pass\n  tls: true\n  username: user\n"
)
_INVALID_LINT = (
    "domain_prefixes: ffmuc_\nBAD_KEY_FOR_DOMAIN:\n- a\n- b\nmqtt:\n  broker_port: 1883\n  broker_url: "
    "mqtt://broker\n  keepalive: 5\n  password: pass\n  tls: true\n  username: user\n"
)
_INVALID_CFG = "asdasdasdasd"


class TestConfig(unittest.TestCase):
    def tearDown(self) -> None:
        config._parsed_config = None
        return super().tearDown()

    def test_load_config_success(self):
        """Test loads and lint config successfully."""
        mock_open = mock.mock_open(read_data=_VALID_CFG)
        with mock.patch("builtins.open", mock_open):
            self.assertDictEqual(yaml.safe_load(_VALID_CFG), config.get_config().raw)

    @mock.patch.object(config.sys, "exit", autospec=True)
    def test_load_config_fails_good_yaml_bad_format(self, exit_mock):
        """Test loads yaml successfully and fails lint."""
        mock_open = mock.mock_open(read_data=_INVALID_LINT)
        with mock.patch("builtins.open", mock_open):
            config.get_config()
            exit_mock.assert_called_with(2)

    @mock.patch.object(config.sys, "exit", autospec=True)
    def test_load_config_fails_bad_yaml(self, exit_mock):
        """Test loads bad YAML."""
        mock_open = mock.mock_open(read_data=_INVALID_CFG)
        with mock.patch("builtins.open", mock_open):
            config.get_config()
            exit_mock.assert_called_with(2)

    def test_fetch_config_from_disk_success(self):
        """Test fetch file from disk."""
        mock_open = mock.mock_open(read_data=_VALID_CFG)
        with mock.patch("builtins.open", mock_open):
            self.assertEqual(config.fetch_config_from_disk(), _VALID_CFG)

    def test_fetch_config_from_disk_fails_file_not_found(self):
        """Test fails on file not found on disk."""
        mock_open = mock.mock_open()
        mock_open.side_effect = FileNotFoundError
        with mock.patch("builtins.open", mock_open):
            with self.assertRaises(config.ConfigFileNotFoundError):
                config.fetch_config_from_disk()

    def test_raw_get_success(self):
        """Test fetch key from configuration."""
        mock_open = mock.mock_open(read_data=_VALID_CFG)
        with mock.patch("builtins.open", mock_open):
            self.assertListEqual(["a", "b"], config.get_config().raw.get("domains"))

    def test_raw_get_no_key_in_config(self):
        """Test fetch non-existent key from configuration."""
        mock_open = mock.mock_open(read_data=_VALID_CFG)
        with mock.patch("builtins.open", mock_open):
            self.assertIsNone(config.get_config().raw.get("key_does_not_exist"))

    def test_key_whitelist_success(self):
        """Test key_whitelist is properly parsed when present."""
        # Using valid WireGuard public key format (44 chars: 42 base64 + 1 special + =)
        cfg_with_whitelist = (
            "domain_prefixes:\n- ffmuc_\ndomains:\n- a\nmqtt:\n  broker_port: 1883"
            "\n  broker_url: mqtt://broker\n  keepalive: 5\n  password: pass\n  tls: true\n  username: user\n"
            "key_whitelist:\n- 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='\n- 'BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBE='\n"
        )
        mock_open = mock.mock_open(read_data=cfg_with_whitelist)
        with mock.patch("builtins.open", mock_open):
            self.assertListEqual(
                ["AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=", "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBE="], 
                config.get_config().key_whitelist
            )

    def test_key_whitelist_not_present(self):
        """Test key_whitelist is None when not in config."""
        mock_open = mock.mock_open(read_data=_VALID_CFG)
        with mock.patch("builtins.open", mock_open):
            self.assertIsNone(config.get_config().key_whitelist)

    def test_key_whitelist_invalid_format(self):
        """Test key_whitelist validation rejects invalid keys."""
        cfg_with_invalid_key = (
            "domain_prefixes:\n- ffmuc_\ndomains:\n- a\nmqtt:\n  broker_port: 1883"
            "\n  broker_url: mqtt://broker\n  keepalive: 5\n  password: pass\n  tls: true\n  username: user\n"
            "key_whitelist:\n- 'invalid_key'\n"
        )
        mock_open = mock.mock_open(read_data=cfg_with_invalid_key)
        with mock.patch("builtins.open", mock_open):
            with self.assertRaises(ValueError) as cm:
                config.get_config()
            self.assertIn("Invalid WireGuard public key", str(cm.exception))

    def test_key_whitelist_not_list(self):
        """Test key_whitelist validation rejects non-list values."""
        cfg_with_string = (
            "domain_prefixes:\n- ffmuc_\ndomains:\n- a\nmqtt:\n  broker_port: 1883"
            "\n  broker_url: mqtt://broker\n  keepalive: 5\n  password: pass\n  tls: true\n  username: user\n"
            "key_whitelist: 'not_a_list'\n"
        )
        mock_open = mock.mock_open(read_data=cfg_with_string)
        with mock.patch("builtins.open", mock_open):
            with self.assertRaises(ValueError) as cm:
                config.get_config()
            self.assertIn("must be a list", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
