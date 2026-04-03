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

    def test_worker_location_parsing(self):
        """Test that worker location is correctly parsed from config."""
        worker_cfg = {"weight": 10, "location": "MUC"}
        worker = config.Worker.from_dict(worker_cfg)
        self.assertEqual(worker.weight, 10)
        self.assertEqual(worker.location, "MUC")

    def test_worker_location_optional(self):
        """Test that worker location is optional."""
        worker_cfg = {"weight": 20}
        worker = config.Worker.from_dict(worker_cfg)
        self.assertEqual(worker.weight, 20)
        self.assertIsNone(worker.location)

    def test_workers_get_locations(self):
        """Test getting unique locations from workers."""
        workers_cfg = {
            "worker1": {"weight": 10, "location": "MUC"},
            "worker2": {"weight": 10, "location": "Vienna"},
            "worker3": {"weight": 10, "location": "MUC"},
            "worker4": {"weight": 10},
        }
        workers = config.Workers.from_dict(workers_cfg)
        locations = workers.get_locations()
        self.assertEqual(locations, ["MUC", "Vienna"])

    def test_workers_get_locations_empty(self):
        """Test getting locations when no locations are configured."""
        workers_cfg = {
            "worker1": {"weight": 10},
            "worker2": {"weight": 10},
        }
        workers = config.Workers.from_dict(workers_cfg)
        locations = workers.get_locations()
        self.assertEqual(locations, [])

    def test_workers_get_workers_by_location(self):
        """Test filtering workers by location."""
        workers_cfg = {
            "worker1": {"weight": 10, "location": "MUC"},
            "worker2": {"weight": 10, "location": "Vienna"},
            "worker3": {"weight": 10, "location": "MUC"},
            "worker4": {"weight": 10},
        }
        workers = config.Workers.from_dict(workers_cfg)
        muc_workers = workers.get_workers_by_location("MUC")
        self.assertEqual(set(muc_workers), {"worker1", "worker3"})

    def test_workers_get_workers_by_location_none(self):
        """Test filtering workers by location with no matches."""
        workers_cfg = {
            "worker1": {"weight": 10, "location": "MUC"},
            "worker2": {"weight": 10, "location": "Vienna"},
        }
        workers = config.Workers.from_dict(workers_cfg)
        berlin_workers = workers.get_workers_by_location("Berlin")
        self.assertEqual(berlin_workers, [])


if __name__ == "__main__":
    unittest.main()
