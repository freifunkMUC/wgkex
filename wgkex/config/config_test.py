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


def _config_dict() -> dict:
    return {
        "domains": ["_test_domain"],
        "domain_prefixes": ["_test_"],
        "mqtt": {"broker_url": "", "username": "", "password": ""},
    }


def _parker_dict() -> dict:
    return {
        "enabled": True,
        "464xlat": True,
        "ipam": "json",
        "prefixes": {
            "ipv4": {"clat_subnet": "10.80.96.0/22"},
            "ipv6": {"length": 63},
        },
    }


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

    def test_valid_parker_configs_success(self):
        """Test valid parker configs are accepted."""
        mock_open = mock.mock_open(
            read_data=_VALID_CFG
            + "\nparker:\n  enabled: true\n  ipam: json\n  464xlat: true\n  prefixes:\n    ipv4:\n      clat_subnet: 10.80.96.0/22\n    ipv6:\n      length: 63\nbroker_signing_key: asdfasdfasdf"
        )
        with mock.patch("builtins.open", mock_open):
            cfg = config.get_config()
            self.assertTrue(cfg.parker.enabled)
            self.assertEqual(cfg.parker.ipam, config.Parker.IPAM.JSON)

    def test_invalid_parker_configs_throw(self):
        """Test invalid parker configs throw errors."""
        mock_open = mock.mock_open(read_data=_VALID_CFG + "\nparker:\n  enabled: true")
        with mock.patch("builtins.open", mock_open):
            with self.assertRaises(ValueError):
                config.get_config()

    def test_parker_ipv6_prefix_length_must_contain_two_64s(self):
        for prefix_length in (-1, 64, 128, "invalid"):
            with self.subTest(prefix_length=prefix_length):
                mock_open = mock.mock_open(
                    read_data=_VALID_CFG
                    + "\nparker:\n"
                    + "  enabled: true\n"
                    + "  ipam: json\n"
                    + "  464xlat: true\n"
                    + "  prefixes:\n"
                    + "    ipv4:\n"
                    + "      clat_subnet: 10.80.96.0/22\n"
                    + "    ipv6:\n"
                    + f"      length: {prefix_length}\n"
                    + "broker_signing_key: asdfasdfasdf"
                )
                with mock.patch("builtins.open", mock_open):
                    with self.assertRaises(ValueError):
                        config.get_config()
                config._parsed_config = None

    def test_worker_tolerance_bounds_are_validated(self):
        for tolerance in (-1, 101):
            with self.subTest(tolerance=tolerance):
                with self.assertRaisesRegex(ValueError, "between 0 and 100"):
                    config.Workers.from_dict({}, tolerance)

    def test_worker_ids_are_stable_unique_integers(self):
        workers = config.Workers.from_dict(
            {
                "first": {"id": "42"},
                "second": {},
            },
            10,
        )
        self.assertEqual(workers.get("first").id, 42)
        self.assertEqual(workers.get("second").id, 2)

        with self.assertRaisesRegex(ValueError, "Worker IDs must be unique"):
            config.Workers.from_dict(
                {
                    "first": {"id": 2},
                    "second": {},
                },
                10,
            )

        self.assertEqual(
            config.Workers.from_dict(
                {"worker": {"id": 2**32 - 1}},
                10,
            )
            .get("worker")
            .id,
            2**32 - 1,
        )
        for worker_id in (-1, 2**32):
            with (
                self.subTest(worker_id=worker_id),
                self.assertRaisesRegex(ValueError, "unsigned 32-bit"),
            ):
                config.Workers.from_dict({"worker": {"id": worker_id}}, 10)

    def test_worker_explicit_null_values_use_defaults(self):
        workers = config.Workers.from_dict(
            {"worker": {"weight": None, "pop": None}}, 10
        )
        self.assertEqual(workers.get("worker").weight, 1)
        self.assertEqual(workers.get("worker").pop, "")

        workers = config.Workers.from_dict({"worker": {"weight": 0}}, 10)
        self.assertEqual(workers.get("worker").weight, 0)

    def test_cleanup_defaults_and_mode_timeouts(self):
        cleanup = config.Cleanup.from_dict({})
        self.assertEqual(cleanup.interval, 3600)
        self.assertEqual(cleanup.stale_timeout(True), 300)
        self.assertEqual(cleanup.stale_timeout(False), 10800)
        self.assertEqual(cleanup.initial_handshake_grace, 600)

        cfg = _config_dict()
        cfg["cleanup"] = {
            "interval": 30,
            "parker_stale_timeout": 600,
            "legacy_stale_timeout": 7200,
            "initial_handshake_grace": 900,
        }
        parsed = config.Config.from_dict(cfg)
        self.assertEqual(parsed.cleanup.interval, 30)
        self.assertEqual(parsed.cleanup.stale_timeout(True), 600)
        self.assertEqual(parsed.cleanup.stale_timeout(False), 7200)
        self.assertEqual(parsed.cleanup.initial_handshake_grace, 900)

    def test_cleanup_durations_are_positive_and_finite(self):
        fields = (
            "interval",
            "parker_stale_timeout",
            "legacy_stale_timeout",
            "initial_handshake_grace",
        )
        for field in fields:
            for value in (0, -1, True, "invalid", float("inf"), None):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaisesRegex(ValueError, field),
                ):
                    config.Cleanup.from_dict({field: value})
        with self.assertRaisesRegex(ValueError, "mapping"):
            config.Cleanup.from_dict([])  # type: ignore

    def test_parker_prefix_structure_validation(self):
        invalid_configs = [
            {"enabled": True, "464xlat": True, "ipam": "json"},
            {
                "enabled": True,
                "464xlat": True,
                "ipam": "json",
                "prefixes": [],
            },
            {
                "enabled": True,
                "464xlat": True,
                "ipam": "json",
                "prefixes": {"ipv4": {}},
            },
            {
                "enabled": True,
                "464xlat": True,
                "ipam": "json",
                "prefixes": {"ipv4": {}, "ipv6": {"length": 63}},
            },
            {
                "enabled": True,
                "464xlat": True,
                "ipam": "json",
                "prefixes": {
                    "ipv4": {"clat_subnet": "10.80.96.0/22"},
                    "ipv6": {},
                },
            },
        ]
        for parker in invalid_configs:
            with self.subTest(parker=parker), self.assertRaises(ValueError):
                config.Parker.from_dict(parker)

    def test_netbox_and_non_xlat_validation(self):
        parker = _parker_dict()
        parker["ipam"] = "netbox"
        with self.assertRaisesRegex(ValueError, "ipv6"):
            config.Parker.from_dict(parker)

        parker["prefixes"]["ipv6"]["netbox_filter"] = {"role": "wgkex"}
        parsed = config.Parker.from_dict(parker)
        self.assertEqual(parsed.ipam, config.Parker.IPAM.NETBOX)

        parker["464xlat"] = False
        parker["prefixes"]["ipv4"] = {}
        with self.assertRaisesRegex(ValueError, "length"):
            config.Parker.from_dict(parker)

        parker["prefixes"]["ipv4"]["length"] = 24
        with self.assertRaisesRegex(ValueError, "netbox_filter"):
            config.Parker.from_dict(parker)

        parker["prefixes"]["ipv4"]["netbox_filter"] = {"role": "wgkex-v4"}
        with self.assertRaises(NotImplementedError):
            config.Parker.from_dict(parker)

    def test_worker_parker_config_does_not_require_broker_credentials(self):
        cfg = _config_dict()
        cfg["parker"] = _parker_dict()
        parsed = config.Config.from_dict(cfg)
        self.assertTrue(parsed.parker.enabled)
        self.assertIsNone(parsed.broker_signing_key)
        self.assertIsNone(parsed.netbox)

        cfg["netbox"] = {}
        parsed = config.Config.from_dict(cfg)
        self.assertIsNone(parsed.netbox)

        cfg["broker_signing_key"] = "key"
        cfg["parker"]["ipam"] = "netbox"
        cfg["parker"]["prefixes"]["ipv6"]["netbox_filter"] = {"role": "wgkex"}
        parsed = config.Config.from_dict(cfg)
        self.assertIsNone(parsed.netbox)

        cfg["netbox"] = {"url": "https://netbox", "api_key": "token"}
        parsed = config.Config.from_dict(cfg)
        self.assertEqual(parsed.netbox.url, "https://netbox")
        self.assertEqual(parsed.netbox.api_key, "token")

    def test_yaml_parser_error_exits(self):
        with (
            mock.patch.object(
                config.yaml, "safe_load", side_effect=yaml.YAMLError("invalid")
            ),
            mock.patch.object(config.sys, "exit", side_effect=SystemExit(1)) as exit,
            mock.patch("builtins.open", mock.mock_open(read_data="invalid")),
            self.assertRaises(SystemExit),
        ):
            config.get_config()
        exit.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
