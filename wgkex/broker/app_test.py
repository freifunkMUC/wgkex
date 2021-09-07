import unittest
import mock
import app
import sys
from wgkex.config.config_test import _VALID_CFG
from wgkex.config.config_test import _INVALID_CFG


class TestApp(unittest.TestCase):

    # TODO(ruairi): Add test for Flask.
    # def setUp(self) -> None:
    # mock_open = mock.mock_open(read_data=_VALID_CFG)
    # with mock.patch("builtins.open", mock_open):
    #     app_cfg = app.app.test_client()
    #     app.main()
    # self.app_cfg = app_cfg

    def test_app_load_success(self):
        """Tests _fetch_app_config success."""
        mock_open = mock.mock_open(read_data=_VALID_CFG)
        with mock.patch("builtins.open", mock_open):
            cfg = app._fetch_app_config()
            self.assertIsNotNone(cfg)

    @mock.patch.object(sys, "exit", autospec=True)
    def test_app_load_fails_bad_config(self, exit_mock):
        """Tests _fetch_app_config fails with bad configuration."""
        mock_open = mock.mock_open(read_data=_INVALID_CFG)
        with mock.patch("builtins.open", mock_open):
            with self.assertRaises(TypeError):
                app._fetch_app_config()
                exit_mock.assert_called_with(2)

    def test_is_valid_wg_pubkey_success(self):
        """Tests is_valid_wg_pubkey success."""
        key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAE="
        self.assertEqual(key, app.is_valid_wg_pubkey(key))

    def test_is_valid_wg_pubkey_fails_bad_key(self):
        """Tests is_valid_wg_pubkey fails on bad key."""
        key = "not_a_key"
        with self.assertRaises(ValueError):
            app.is_valid_wg_pubkey(key)

    def test_is_valid_domain_success(self):
        """Tests is_valid_domain success."""
        domain = "a"
        mock_open = mock.mock_open(read_data=_VALID_CFG)
        with mock.patch("builtins.open", mock_open):
            self.assertEqual(domain, app.is_valid_domain(domain))

    def test_is_valid_domain_fails_domain_not_configured(self):
        """Tests is_valid_domain fails on bad domain."""
        domain = "not_Configured"
        mock_open = mock.mock_open(read_data=_VALID_CFG)
        with mock.patch("builtins.open", mock_open):
            with self.assertRaises(ValueError):
                app.is_valid_domain(domain)

    def test_KeyExchange_success(self):
        """Tests creating KeyExchange successfully."""
        key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAE="
        msg = dict(public_key=key, domain="a")
        expected = app.KeyExchange(public_key=key, domain="a")
        mock_open = mock.mock_open(read_data=_VALID_CFG)
        with mock.patch("builtins.open", mock_open):
            self.assertEqual(expected, app.KeyExchange.from_dict(msg))

    def test_KeyExchange_fails_bad_key(self):
        """Tests creating KeyExchange fails due to bad key."""
        key = "asd"
        msg = dict(public_key=key, domain="a")
        mock_open = mock.mock_open(read_data=_VALID_CFG)
        with mock.patch("builtins.open", mock_open):
            with self.assertRaises(ValueError):
                app.KeyExchange.from_dict(msg)

    def test_KeyExchange_fails_bad_domain(self):
        """Tests creating KeyExchange fails due to bad key."""
        key = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAE="
        msg = dict(public_key=key, domain="unconfigured_domain")
        mock_open = mock.mock_open(read_data=_VALID_CFG)
        with mock.patch("builtins.open", mock_open):
            with self.assertRaises(ValueError):
                app.KeyExchange.from_dict(msg)


if __name__ == "__main__":
    unittest.main()
