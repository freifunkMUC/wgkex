import unittest
import mock
import config
import yaml

_VALID_CFG = "domains:\n- a\n- b\nmqtt:\n  broker_port: 1883\n  broker_url: mqtt://broker\n  keepalive: 5\n  password: pass\n  tls: true\n  username: user\n"
_INVALID_LINT = "derpmains:\n- a\n- b\nmqtt:\n  broker_port: 1883\n  broker_url: mqtt://broker\n  keepalive: 5\n  password: pass\n  tls: true\n  username: user\n"
_INVALID_CFG = "asdasdasdasd"


class TestConfig(unittest.TestCase):
    def test_load_config_success(self):
        """Test loads and lint config successfully."""
        mock_open = mock.mock_open(read_data=_VALID_CFG)
        with mock.patch("builtins.open", mock_open):
            self.assertDictEqual(yaml.safe_load(_VALID_CFG), config.load_config())

    @mock.patch.object(config.sys, "exit", autospec=True)
    def test_load_config_fails_good_yaml_bad_format(self, exit_mock):
        """Test loads yaml successfully and fails lint."""
        mock_open = mock.mock_open(read_data=_INVALID_LINT)
        with mock.patch("builtins.open", mock_open):
            config.load_config()
            exit_mock.assert_called_with(2)

    @mock.patch.object(config.sys, "exit", autospec=True)
    def test_load_config_fails_bad_yaml(self, exit_mock):
        """Test loads bad YAML."""
        mock_open = mock.mock_open(read_data=_INVALID_CFG)
        with mock.patch("builtins.open", mock_open):
            config.load_config()
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

    def test_fetch_from_config_success(self):
        """Test fetch key from configuration."""
        mock_open = mock.mock_open(read_data=_VALID_CFG)
        with mock.patch("builtins.open", mock_open):
            self.assertListEqual(["a", "b"], config.fetch_from_config("domains"))

    def test_fetch_from_config_no_key_in_config(self):
        """Test fetch non existent key from configuration."""
        mock_open = mock.mock_open(read_data=_VALID_CFG)
        with mock.patch("builtins.open", mock_open):
            self.assertIsNone(config.fetch_from_config("key_does_not_exist"))


if __name__ == "__main__":
    unittest.main()
