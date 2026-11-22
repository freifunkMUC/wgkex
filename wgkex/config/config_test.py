"""Tests for configuration handling class."""

import ipaddress
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

    def test_metrics(self):
        """The broker endpoint is on and the worker listener off by default."""
        self.assertEqual(
            config.Metrics.from_dict({}), config.Metrics(True, None, "0.0.0.0")
        )
        self.assertEqual(
            config.Metrics.from_dict(
                {"broker_endpoint": False, "worker_port": "9101", "worker_host": "::"}
            ),
            config.Metrics(False, 9101, "::"),
        )

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

    def test_phase_f_defaults_and_parsing(self):
        cfg = config.Config.from_dict(yaml.safe_load(_VALID_CFG))
        self.assertEqual(cfg.rate_limit, config.RateLimit())
        self.assertEqual(cfg.key_lists, config.KeyLists())
        self.assertFalse(cfg.load_balancing.share_pending_assignments)
        cfg = config.Config.from_dict(
            {
                **yaml.safe_load(_VALID_CFG),
                "workers": {"gw1": {"weight": 2, "location": "muc"}, "gw2": {}},
                "rate_limit": {"per_client_requests": 5, "ipv6_prefix_length": 200},
                "key_lists": {"blocklist": ["k1"], "allowlist_file": "/tmp/x"},
                "load_balancing": {"location_tolerance": 0.2, "broker_id": "b1"},
            }
        )
        self.assertEqual(cfg.workers.location("gw1"), "muc")
        self.assertIsNone(cfg.workers.location("gw2"))
        self.assertIsNone(cfg.workers.location("unknown"))
        self.assertEqual(cfg.workers.get("gw2").weight, 1)
        self.assertEqual(cfg.rate_limit.per_client_requests, 5)
        self.assertEqual(cfg.rate_limit.ipv6_prefix_length, 128)
        self.assertEqual(cfg.key_lists.blocklist, ["k1"])
        self.assertEqual(cfg.key_lists.allowlist_file, "/tmp/x")
        self.assertEqual(cfg.load_balancing.location_tolerance, 0.2)
        self.assertEqual(cfg.load_balancing.broker_id, "b1")

    def test_broker_trusted_proxies(self):
        base = yaml.safe_load(_VALID_CFG)
        cfg = config.Config.from_dict(base)
        self.assertEqual(cfg.broker.trusted_proxies, 0)
        self.assertFalse(cfg.broker.log_client_ip)
        for value, expected in (
            (2, 2),
            (-1, 0),
            (None, 0),
            ("10.0.0.1", [ipaddress.ip_network("10.0.0.1/32")]),
            (
                ["10.0.0.5/24", "2001:db8::/64"],
                [
                    ipaddress.ip_network("10.0.0.0/24"),
                    ipaddress.ip_network("2001:db8::/64"),
                ],
            ),
        ):
            cfg = config.Config.from_dict(
                {**base, "broker": {"trusted_proxies": value, "log_client_ip": True}}
            )
            self.assertEqual(cfg.broker.trusted_proxies, expected)
            self.assertTrue(cfg.broker.log_client_ip)

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
        with mock.patch("builtins.open", mock_open), self.assertRaises(SystemExit):
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
                with (
                    mock.patch("builtins.open", mock_open),
                    self.assertRaises(SystemExit),
                ):
                    config.get_config()
                config._parsed_config = None

    def test_worker_tolerance_bounds_are_validated(self):
        for tolerance in (-1, 101):
            with (
                self.subTest(tolerance=tolerance),
                self.assertRaisesRegex(ValueError, "between 0 and 100"),
            ):
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
            {"worker": {"weight": None, "pop": None, "endpoint_name": None}}, 10
        )
        self.assertEqual(workers.get("worker").weight, 1)
        self.assertEqual(workers.get("worker").pop, "")
        self.assertIsNone(workers.get("worker").endpoint_name)

    def test_worker_endpoint_name_is_optional_string(self):
        workers = config.Workers.from_dict(
            {
                "named": {"endpoint_name": "gw1.lab"},
                "empty": {"endpoint_name": ""},
                "unset": {},
            },
            10,
        )
        self.assertEqual(workers.get("named").endpoint_name, "gw1.lab")
        self.assertIsNone(workers.get("empty").endpoint_name)
        self.assertIsNone(workers.get("unset").endpoint_name)

    def test_peer_cleanup_defaults_and_mode_timeouts(self):
        cleanup = config.PeerCleanup.from_dict({})
        self.assertEqual(cleanup.interval, 300)
        self.assertEqual(cleanup.timeout_for(True), 300)
        self.assertEqual(cleanup.timeout_for(False), 1800)
        self.assertEqual(cleanup.initial_handshake_grace, 600)

        cfg = _config_dict()
        cfg["peer_cleanup"] = {
            "interval": 30,
            "parker_stale_timeout": 600,
            "stale_timeout": 7200,
            "initial_handshake_grace": 900,
        }
        parsed = config.Config.from_dict(cfg)
        self.assertEqual(parsed.peer_cleanup.interval, 30)
        self.assertEqual(parsed.peer_cleanup.timeout_for(True), 600)
        self.assertEqual(parsed.peer_cleanup.timeout_for(False), 7200)
        self.assertEqual(parsed.peer_cleanup.initial_handshake_grace, 900)

    def test_parker_cleanup_section_is_accepted_as_alias(self):
        cfg = _config_dict()
        cfg["cleanup"] = {
            "interval": 3600,
            "parker_stale_timeout": 600,
            "legacy_stale_timeout": 10800,
            "initial_handshake_grace": 900,
        }
        parsed = config.Config.from_dict(cfg).peer_cleanup
        self.assertEqual(parsed.interval, 3600)
        self.assertEqual(parsed.timeout_for(True), 600)
        self.assertEqual(parsed.timeout_for(False), 10800)
        self.assertEqual(parsed.initial_handshake_grace, 900)

        # peer_cleanup wins over the alias
        cfg["peer_cleanup"] = {"interval": 60, "initial_handshake_grace": 120}
        parsed = config.Config.from_dict(cfg).peer_cleanup
        self.assertEqual(parsed.interval, 60)
        self.assertEqual(parsed.initial_handshake_grace, 120)
        self.assertEqual(parsed.timeout_for(True), 600)

        with self.assertRaisesRegex(ValueError, "mapping"):
            config.PeerCleanup.from_dict({}, ["invalid"])  # type: ignore

    def test_parker_cleanup_durations_are_positive_and_finite(self):
        for field in ("parker_stale_timeout", "initial_handshake_grace"):
            for value in (0, -1, True, "invalid", float("inf"), None):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaisesRegex(ValueError, field),
                ):
                    config.PeerCleanup.from_dict({field: value})

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

    def test_parker_pgw_selection(self):
        parker = _parker_dict()
        parker["pgw_selection"] = {"rebalance_max_moves": 0, "rebalance_window": 60}
        parsed = config.Parker.from_dict(parker).pgw_selection
        self.assertEqual(parsed, config.Parker.PgwSelection(0.2, 10, 0, 60))
        for bad in (
            {"rebalance_tolerance": -1},
            {"rebalance_max_moves": -1},
            {"rebalance_window": 0},
            [],
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                config.Parker.from_dict({**parker, "pgw_selection": bad})

    def test_parker_sites_and_route_gate(self):
        parker = _parker_dict()
        parker["ipam"] = "netbox"
        parker["sites"] = {
            "muc": {"tunnel_id": 1, "netbox_filter": {"role": "muc"}},
            "vie": {
                "tunnel_id": 2,
                "netbox_filter": {"role": "vie"},
                "capacity": 500,
                "weight": 2,
            },
        }
        parker["route_gate"] = {"handshake_max_age": 120}
        # Sites replace the global IPv6 NetBox filter.
        parsed = config.Parker.from_dict(parker)
        self.assertEqual(parsed.sites["vie"].netbox_filter, {"role": "vie"})
        self.assertEqual(
            (parsed.sites["muc"].capacity, parsed.sites["muc"].weight), (None, 1)
        )
        self.assertEqual(
            (parsed.sites["vie"].capacity, parsed.sites["vie"].weight), (500, 2)
        )
        self.assertEqual(parsed.route_gate, config.Parker.RouteGate(120, 1, 16))
        self.assertEqual(
            config.Parker.from_dict(
                {**parker, "route_gate": {"route_metric": 8}}
            ).route_gate.route_metric,
            8,
        )
        for metric in (0, -1, "16", True, 2**32):
            with (
                self.subTest(metric=metric),
                self.assertRaisesRegex(ValueError, "route_metric"),
            ):
                config.Parker.from_dict(
                    {**parker, "route_gate": {"route_metric": metric}}
                )
        self.assertIsNone(config.Parker.from_dict(_parker_dict()).route_gate)

        for site, error in (
            ({"netbox_filter": {}}, "tunnel_id"),
            ({"tunnel_id": 1}, "netbox_filter"),
            ({"tunnel_id": 3, "netbox_filter": {}, "capacity": -1}, "capacity"),
            ({"tunnel_id": 3, "netbox_filter": {}, "weight": 0}, "weight"),
            ({"tunnel_id": 2, "netbox_filter": {}}, "unique"),
        ):
            with (
                self.subTest(error=error),
                self.assertRaisesRegex(ValueError, error),
            ):
                config.Parker.from_dict(
                    {**parker, "sites": {**parker["sites"], "ber": site}}
                )
        with self.assertRaisesRegex(ValueError, "interval"):
            config.Parker.from_dict({**parker, "route_gate": {"interval": 0}})

    def test_parker_ipam_cache(self):
        parsed = config.Parker.from_dict(_parker_dict())
        self.assertEqual(parsed.ipam_cache, config.Parker.IPAMCache(None, 60, 32))
        parsed = config.Parker.from_dict(
            {
                **_parker_dict(),
                "ipam_cache": {
                    "snapshot_dir": "/data",
                    "sync_interval": 30,
                    "reserve": 8,
                    "reclaim_days": 90,
                    "reclaim_dry_run": False,
                },
            }
        )
        self.assertEqual(
            parsed.ipam_cache, config.Parker.IPAMCache("/data", 30, 8, 90, False)
        )
        for cache, error in (
            ([], "mapping"),
            ({"reserve": -1}, "reserve"),
            ({"sync_interval": 0}, "sync_interval"),
            ({"reclaim_days": -1}, "reclaim_days"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                config.Parker.from_dict({**_parker_dict(), "ipam_cache": cache})

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

    def test_apply_env_overrides(self):
        """Test WGKEX__ variables override and extend the configuration."""
        base = yaml.safe_load(_VALID_CFG)
        base["externalName"] = "gw.example.org"
        env = {
            "WGKEX__MQTT__PASSWORD": "0123",
            "WGKEX__MQTT__TLS": "False",
            "WGKEX__MQTT__KEEPALIVE": "20",
            "WGKEX__DOMAINS": "[c, d]",
            "WGKEX__WORKERS": "{gw1.example.org: {weight: 25}}",
            "WGKEX__LOAD_BALANCING__REBALANCING__ENABLED": "true",
            "WGKEX__EXTERNALNAME": "gw2.example.org",
            "OTHER": "ignored",
        }
        result = config.apply_env_overrides(base, env)
        self.assertEqual(result["mqtt"]["password"], "0123")
        self.assertIs(result["mqtt"]["tls"], False)
        self.assertEqual(result["mqtt"]["username"], "user")
        self.assertEqual(result["domains"], ["c", "d"])
        self.assertEqual(result["externalName"], "gw2.example.org")
        self.assertNotIn("externalname", result)
        self.assertEqual(base["mqtt"]["password"], "pass")
        cfg = config.Config.from_dict(result)
        self.assertEqual(cfg.mqtt.keepalive, 20)
        self.assertFalse(cfg.mqtt.tls)
        self.assertEqual(cfg.workers.get("gw1.example.org").weight, 25)
        self.assertTrue(cfg.load_balancing.rebalancing.enabled)

    def test_apply_env_overrides_credentials_stay_strings(self):
        env = {
            "WGKEX__MQTT__USERNAME": "{x}",
            "WGKEX__MQTT__PASSWORD": "true",
            "WGKEX__MQTT__TLS": "no",
        }
        result = config.apply_env_overrides(yaml.safe_load(_VALID_CFG), env)
        self.assertEqual(result["mqtt"]["username"], "{x}")
        self.assertEqual(result["mqtt"]["password"], "true")
        self.assertIs(result["mqtt"]["tls"], False)

    def test_apply_env_overrides_error_hides_value(self):
        with self.assertRaises(ValueError) as ctx:
            config.apply_env_overrides({}, {"WGKEX__WORKERS": "{secret-part"})
        self.assertNotIn("secret-part", str(ctx.exception))
        self.assertIn("WGKEX__WORKERS", str(ctx.exception))

    @mock.patch.dict(
        config.os.environ,
        {
            "WGKEX__DOMAINS": "[a]",
            "WGKEX__DOMAIN_PREFIXES": "[ffmuc_]",
            "WGKEX__MQTT__BROKER_URL": "mqtt",
            "WGKEX__MQTT__USERNAME": "u",
            "WGKEX__MQTT__PASSWORD": "p",
        },
    )
    def test_load_config_empty_file_with_env(self):
        mock_open = mock.mock_open(read_data="")
        with mock.patch("builtins.open", mock_open):
            self.assertEqual(config.get_config().domains, ["a"])

    def test_apply_env_overrides_nested_wins_over_parent(self):
        env = {
            "WGKEX__BROKER": "{log_client_ip: true, trusted_proxies: 1}",
            "WGKEX__BROKER__TRUSTED_PROXIES": "[10.0.0.0/8]",
        }
        result = config.apply_env_overrides({}, env)
        self.assertEqual(
            result["broker"], {"log_client_ip": True, "trusted_proxies": ["10.0.0.0/8"]}
        )

    def test_apply_env_overrides_errors(self):
        for env in (
            {"WGKEX__MQTT____PASSWORD": "x"},
            {"WGKEX__DOMAINS__X": "x"},
            {"WGKEX__WORKERS": "{broken"},
        ):
            with self.assertRaises(ValueError):
                config.apply_env_overrides(yaml.safe_load(_VALID_CFG), env)

    @mock.patch.dict(
        config.os.environ,
        {
            "WGKEX__DOMAINS": "[a]",
            "WGKEX__DOMAIN_PREFIXES": "[ffmuc_]",
            "WGKEX__MQTT__BROKER_URL": "mqtt",
            "WGKEX__MQTT__USERNAME": "u",
            "WGKEX__MQTT__PASSWORD": "p",
            "WGKEX__BROKER_LISTEN__PORT": "5000",
        },
    )
    def test_load_config_from_env_without_file(self):
        """Test the configuration can come from the environment alone."""
        mock_open = mock.mock_open()
        mock_open.side_effect = FileNotFoundError
        with mock.patch("builtins.open", mock_open):
            cfg = config.get_config()
        self.assertEqual(cfg.mqtt.password, "p")
        self.assertEqual(cfg.broker_listen.port, 5000)

    def test_load_config_without_file_or_env_fails(self):
        mock_open = mock.mock_open()
        mock_open.side_effect = FileNotFoundError
        with (
            mock.patch.dict(config.os.environ, clear=True),
            mock.patch("builtins.open", mock_open),
            self.assertRaises(config.ConfigFileNotFoundError),
        ):
            config.get_config()

    @mock.patch.object(config.sys, "exit", autospec=True)
    @mock.patch.dict(config.os.environ, {"WGKEX__DOMAINS__X": "y"})
    def test_load_config_bad_env_override_exits(self, exit_mock):
        mock_open = mock.mock_open(read_data=_VALID_CFG)
        with mock.patch("builtins.open", mock_open):
            config.get_config()
        exit_mock.assert_any_call(1)


if __name__ == "__main__":
    unittest.main()
