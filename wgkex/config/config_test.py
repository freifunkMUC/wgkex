"""Tests for configuration handling class."""

import unittest
from unittest import mock

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

    def test_load_balancing_defaults(self):
        """Load balancing settings default to rebalancing disabled."""
        cfg = config.Config.from_dict(yaml.safe_load(_VALID_CFG))
        self.assertEqual(cfg.load_balancing, config.LoadBalancing())
        self.assertFalse(cfg.load_balancing.rebalancing.enabled)
        self.assertEqual(cfg.load_balancing.pending_assignment_ttl, 300)

    def test_load_balancing_from_config(self):
        """Load balancing settings are read from the configuration file."""
        raw = yaml.safe_load(
            _VALID_CFG
            + "load_balancing:\n  pending_assignment_ttl: 120\n  pending_assignment_settle_time: 30\n"
            "  rebalancing:\n    enabled: true\n    interval: 30\n    tolerance: 0.2\n"
            "    min_excess_peers: 5\n    stable_time: 300\n    max_evictions_ratio: 0.05\n"
            "    eviction_cooldown: 600\n"
        )
        lb = config.Config.from_dict(raw).load_balancing
        self.assertEqual(
            lb,
            config.LoadBalancing(
                pending_assignment_ttl=120,
                pending_assignment_settle_time=30,
                rebalancing=config.Rebalancing(
                    enabled=True,
                    interval=30,
                    tolerance=0.2,
                    min_excess_peers=5,
                    stable_time=300,
                    max_evictions_ratio=0.05,
                    eviction_cooldown=600,
                ),
            ),
        )

    def test_targeted_publish_and_peer_cleanup(self):
        """Targeted publishing and peer cleanup options are parsed with defaults."""
        cfg = config.LoadBalancing.from_dict({})
        self.assertTrue(cfg.targeted_publish)
        self.assertEqual(cfg.previous_assignment_ttl, 21600)
        cfg = config.LoadBalancing.from_dict(
            {"targeted_publish": False, "previous_assignment_max_entries": 5}
        )
        self.assertFalse(cfg.targeted_publish)
        self.assertEqual(cfg.previous_assignment_max_entries, 5)
        self.assertEqual(
            config.PeerCleanup.from_dict({}), config.PeerCleanup(1800, 300)
        )
        self.assertEqual(
            config.PeerCleanup.from_dict({"stale_timeout": 60, "interval": 10}),
            config.PeerCleanup(60, 10),
        )

    def test_drain(self):
        """Drain options are parsed with defaults, the drain rate is clamped."""
        self.assertEqual(config.Drain.from_dict({}), config.Drain(None, 5))
        self.assertEqual(
            config.Drain.from_dict({"file": "/run/wgkex/drain", "check_interval": 0}),
            config.Drain("/run/wgkex/drain", 1),
        )
        self.assertEqual(config.Rebalancing.from_dict({}).drain_rate, 0.1)
        self.assertEqual(config.Rebalancing.from_dict({"drain_rate": 3}).drain_rate, 1)

    def test_relative_worker_weight_unknown_worker(self):
        """Workers missing from the config get the average weight."""
        workers = config.Workers.from_dict({"a": {"weight": 10}, "b": {"weight": 30}})
        self.assertEqual(workers.relative_worker_weight("a"), 0.25)
        self.assertEqual(workers.relative_worker_weight("unknown"), 0.5)
        self.assertEqual(config.Workers.from_dict({}).relative_worker_weight("x"), 1)

    def test_fetch_config_from_disk_success(self):
        """Test fetch file from disk."""
        mock_open = mock.mock_open(read_data=_VALID_CFG)
        with mock.patch("builtins.open", mock_open):
            self.assertEqual(config.fetch_config_from_disk(), _VALID_CFG)

    def test_fetch_config_from_disk_fails_file_not_found(self):
        """Test fails on file not found on disk."""
        mock_open = mock.mock_open()
        mock_open.side_effect = FileNotFoundError
        with (
            mock.patch("builtins.open", mock_open),
            self.assertRaises(config.ConfigFileNotFoundError),
        ):
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


if __name__ == "__main__":
    unittest.main()
