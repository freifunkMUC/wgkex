"""Tests for the broker HTTP API, especially its compatibility with the Gluon client.

The Gluon package ffmuc-mesh-vpn-wireguard-vxlan relies on the exact response shapes asserted here.
"""

import importlib
import ipaddress
import json
import sys
import unittest
from unittest import mock

from wgkex.common.mqtt import CONNECTED_PEERS_METRIC
from wgkex.config import config

_DOMAIN = "ffmuc_welt"
_PUBKEY = "TszFS3oFRdhsJP3K0VOlklGMGYZy+oFCtlaghXJqW2g="
_PUBKEY2 = "A" * 42 + "E="


def _decorator_factory(*args, **kwargs):
    return lambda f: f


class BrokerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        config._parsed_config = config.Config.from_dict(
            {
                "domains": [_DOMAIN],
                "domain_prefixes": ["ffmuc_"],
                "workers": {},
                "mqtt": {"broker_url": "", "username": "", "password": ""},
            }
        )
        mqtt_mock = mock.MagicMock()
        mqtt_mock.on_connect.side_effect = _decorator_factory
        mqtt_mock.on_topic.side_effect = _decorator_factory
        mqtt_mock.on_message.side_effect = _decorator_factory
        cls.mqtt_patcher = mock.patch("flask_mqtt.Mqtt", return_value=mqtt_mock)
        cls.mqtt_patcher.start()
        sys.modules.pop("wgkex.broker.app", None)
        from wgkex.broker import app

        cls.app = app

    @classmethod
    def tearDownClass(cls) -> None:
        cls.mqtt_patcher.stop()
        config._parsed_config = None

    def setUp(self) -> None:
        self.app.worker_metrics.data.clear()
        self.app.worker_metrics._pending.clear()
        self.app.worker_data.clear()
        self.app.assignment_history._entries.clear()
        self.app.mqtt.reset_mock()
        self.app.rate_limiter = self.app._create_rate_limiter()
        self.app.blocklist = self.app._create_blocklist()
        self.client = self.app.app.test_client()

    def add_worker(self, worker: str, peers: int, with_data: bool = True) -> None:
        self.app.worker_metrics.update(worker, _DOMAIN, CONNECTED_PEERS_METRIC, peers)
        self.app.worker_metrics.set_online(worker)
        if with_data:
            self.app.worker_data[(worker, _DOMAIN)] = {
                "ExternalAddress": f"{worker}.ext.example.net",
                "Port": 51820,
                "PublicKey": "WORKERPUBKEY=",
                "LinkAddress": "fe80::1",
            }

    def post(self, version: str, body: dict):
        # The Gluon client posts with wget --post-data, without a JSON Content-Type header
        return self.client.post(
            f"/api/{version}/wg/key/exchange",
            data=json.dumps(body),
            content_type="application/x-www-form-urlencoded",
        )

    def status(self, worker: str, payload: bytes) -> None:
        m = mock.MagicMock()
        m.topic = f"wireguard-worker/{worker}/status"
        m.payload = payload
        self.app.handle_mqtt_message_status(None, None, m)

    def sample(self, name: str, **labels) -> float:
        value = self.app.observability.REGISTRY.get_sample_value(name, labels)
        return value or 0.0


class BrokerAppTest(BrokerTestCase):
    def test_v1_response_shape(self):
        """v1 responds with exactly {"Message": "OK"} and publishes the key to all workers."""
        resp = self.post("v1", {"domain": _DOMAIN, "public_key": _PUBKEY})
        self.assertEqual(resp.status_code, 200)
        self.assertDictEqual(resp.get_json(), {"Message": "OK"})
        self.app.mqtt.publish.assert_called_once_with(
            f"wireguard/{_DOMAIN}/all", _PUBKEY, qos=1
        )

    def test_v2_response_shape(self):
        """v2 responds with the endpoint in the exact shape the Gluon client parses."""
        self.add_worker("gw1", 10)
        resp = self.post("v2", {"domain": _DOMAIN, "public_key": _PUBKEY})
        self.assertEqual(resp.status_code, 200)
        self.assertDictEqual(
            resp.get_json(),
            {
                "Endpoint": {
                    "Address": "gw1.ext.example.net",
                    "Port": "51820",
                    "PublicKey": "WORKERPUBKEY=",
                    "AllowedIPs": ["fe80::1"],
                }
            },
        )
        endpoint = resp.get_json()["Endpoint"]
        self.assertIsInstance(endpoint["Port"], str)
        self.assertIsInstance(endpoint["AllowedIPs"], list)
        # The key is only published to the chosen worker
        self.app.mqtt.publish.assert_called_once_with(
            f"wireguard/{_DOMAIN}/gw1", _PUBKEY, qos=1
        )

    def test_v2_untargeted_publish(self):
        """With targeted_publish disabled, v2 publishes keys to all workers like before."""
        self.add_worker("gw1", 10)
        with mock.patch.object(
            config.get_config().load_balancing, "targeted_publish", False
        ):
            resp = self.post("v2", {"domain": _DOMAIN, "public_key": _PUBKEY})
        self.assertEqual(resp.status_code, 200)
        self.app.mqtt.publish.assert_called_once_with(
            f"wireguard/{_DOMAIN}/all", _PUBKEY, qos=1
        )

    def test_v2_no_worker_publishes_to_all(self):
        """Without online workers, v2 fails as before and publishes the key to all workers."""
        resp = self.post("v2", {"domain": _DOMAIN, "public_key": _PUBKEY})
        self.assertEqual(resp.status_code, 400)
        self.app.mqtt.publish.assert_called_once_with(
            f"wireguard/{_DOMAIN}/all", _PUBKEY, qos=1
        )

    def test_v2_reassignment_removes_from_previous_worker(self):
        """When a key moves to another worker, the previous worker is asked to remove it."""
        self.add_worker("gw1", 0)
        self.add_worker("gw2", 1000)
        self.post("v2", {"domain": _DOMAIN, "public_key": _PUBKEY})
        self.app.mqtt.publish.assert_called_once_with(
            f"wireguard/{_DOMAIN}/gw1", _PUBKEY, qos=1
        )
        # gw1 goes down, the node reconnects and gets gw2
        self.app.worker_metrics.set_offline("gw1")
        self.app.mqtt.reset_mock()
        self.post("v2", {"domain": _DOMAIN, "public_key": _PUBKEY})
        self.assertEqual(
            self.app.mqtt.publish.call_args_list,
            [
                mock.call(f"wireguard/{_DOMAIN}/gw2", _PUBKEY, qos=1),
                mock.call(f"wireguard-worker/gw1/{_DOMAIN}/remove", _PUBKEY, qos=1),
            ],
        )
        # Same worker again: nothing to remove
        self.app.mqtt.reset_mock()
        self.post("v2", {"domain": _DOMAIN, "public_key": _PUBKEY})
        self.app.mqtt.publish.assert_called_once_with(
            f"wireguard/{_DOMAIN}/gw2", _PUBKEY, qos=1
        )

    def test_v2_after_v1_removes_from_other_workers(self):
        """A key previously published to all workers is removed from all but the chosen one."""
        self.add_worker("gw1", 0)
        self.add_worker("gw2", 1000)
        self.add_worker("gw3", 1000)
        self.post("v1", {"domain": _DOMAIN, "public_key": _PUBKEY})
        self.app.mqtt.reset_mock()
        self.post("v2", {"domain": _DOMAIN, "public_key": _PUBKEY})
        self.assertEqual(
            self.app.mqtt.publish.call_args_list,
            [
                mock.call(f"wireguard/{_DOMAIN}/gw1", _PUBKEY, qos=1),
                mock.call(f"wireguard-worker/gw2/{_DOMAIN}/remove", _PUBKEY, qos=1),
                mock.call(f"wireguard-worker/gw3/{_DOMAIN}/remove", _PUBKEY, qos=1),
            ],
        )

    def test_v2_records_pending_assignment(self):
        """v2 counts the assigned peer for the chosen worker until metrics reflect it."""
        self.add_worker("gw1", 10)
        self.post("v2", {"domain": _DOMAIN, "public_key": _PUBKEY})
        self.assertEqual(self.app.worker_metrics.get_effective_peer_count("gw1"), 11)
        # The reported metric itself is not modified
        self.assertEqual(self.app.worker_metrics.get("gw1").get_peer_count(), 10)

    def test_v2_skips_worker_without_endpoint_data(self):
        """v2 never picks a worker whose endpoint data is unknown, if another one is online."""
        self.add_worker("gw1", 0, with_data=False)
        self.add_worker("gw2", 1000)
        for _ in range(20):
            resp = self.post("v2", {"domain": _DOMAIN, "public_key": _PUBKEY})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(
                resp.get_json()["Endpoint"]["Address"], "gw2.ext.example.net"
            )

    def test_v2_no_worker_online(self):
        """v2 keeps its error behaviour if no worker is online (client falls back to v1)."""
        resp = self.post("v2", {"domain": _DOMAIN, "public_key": _PUBKEY})
        self.assertEqual(resp.status_code, 400)

    def test_invalid_request(self):
        """Invalid keys are rejected as before."""
        for version in ("v1", "v2"):
            resp = self.post(version, {"domain": _DOMAIN, "public_key": "invalid"})
            self.assertEqual(resp.status_code, 400)

    def test_stale_metrics_worker_not_chosen(self):
        """Workers without recent metrics for the domain are treated as offline for it."""
        clock = self.app.worker_metrics.clock
        with mock.patch.object(self.app.worker_metrics, "clock") as clock_mock:
            clock_mock.return_value = clock()
            self.add_worker("gw1", 0)
            clock_mock.return_value = clock() + 1000
            self.add_worker("gw2", 1000)
            for _ in range(20):
                resp = self.post("v2", {"domain": _DOMAIN, "public_key": _PUBKEY})
                self.assertEqual(
                    resp.get_json()["Endpoint"]["Address"], "gw2.ext.example.net"
                )

    def test_mqtt_handlers_survive_bad_payloads(self):
        """Invalid payloads from MQTT are ignored without raising."""
        self.add_worker("gw1", 10)

        def msg(topic, payload):
            m = mock.MagicMock()
            m.topic = topic
            m.payload = payload
            return m

        self.app.handle_mqtt_message_metrics(
            None, None, msg(f"wireguard-metrics/{_DOMAIN}/gw1/connected_peers", b"x")
        )
        self.app.handle_mqtt_message_metrics(
            None, None, msg("wireguard-metrics/short", b"1")
        )
        self.app.handle_mqtt_message_status(
            None, None, msg("wireguard-worker/gw1/status", b"\xff")
        )
        self.app.handle_mqtt_message_data(
            None, None, msg(f"wireguard-worker/gw1/{_DOMAIN}/data", b"{not json")
        )
        self.app.handle_mqtt_message(None, None, msg("foo", b"\xff\xfe"))
        self.assertEqual(self.app.worker_metrics.get("gw1").get_peer_count(), 10)
        self.assertTrue(self.app.worker_metrics.get("gw1").is_online())
        self.assertIn(("gw1", _DOMAIN), self.app.worker_data)

    def test_publish_rebalance_hints(self):
        """Hints are published as JSON to the new per-worker rebalance topic."""
        clock = self.app.worker_metrics.clock
        with (
            mock.patch.object(self.app.worker_metrics, "clock") as clock_mock,
            mock.patch.object(self.app.worker_metrics, "metrics_max_age", 0),
        ):
            clock_mock.return_value = clock()
            self.add_worker("gw1", 0)
            self.add_worker("gw2", 600)
            clock_mock.return_value = clock() + 3600
            self.app.publish_rebalance_hints()
        self.app.mqtt.publish.assert_called_once_with(
            "wireguard-worker/gw2/rebalance",
            json.dumps({"excess": 300, "domains": [_DOMAIN]}),
            qos=0,
            retain=False,
        )

    def test_draining_status(self):
        """Status 2 keeps a worker online but excludes it from new v2 assignments."""
        self.add_worker("gw1", 0)
        self.add_worker("gw2", 500)
        self.status("gw1", b"2")
        self.assertTrue(self.app.worker_metrics.get("gw1").is_online())
        self.assertTrue(self.app.worker_metrics.is_draining("gw1"))
        for _ in range(10):
            resp = self.post("v2", {"domain": _DOMAIN, "public_key": _PUBKEY})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(
                resp.get_json()["Endpoint"]["Address"], "gw2.ext.example.net"
            )
        self.status("gw1", b"1")
        self.assertFalse(self.app.worker_metrics.is_draining("gw1"))
        self.status("gw1", b"2")
        self.status("gw1", b"0")
        self.assertFalse(self.app.worker_metrics.get("gw1").is_online())
        self.assertFalse(self.app.worker_metrics.is_draining("gw1"))

    def test_publish_drain_hints(self):
        """Draining workers get hints with the configured drain rate."""
        self.add_worker("gw1", 40)
        self.add_worker("gw2", 0)
        self.status("gw1", b"2")
        with (
            mock.patch.object(self.app.worker_metrics, "metrics_max_age", 0),
            mock.patch.object(
                config.get_config().load_balancing.rebalancing, "drain_rate", 0.25
            ),
        ):
            self.app.publish_rebalance_hints()
        self.app.mqtt.publish.assert_called_once_with(
            "wireguard-worker/gw1/rebalance",
            json.dumps(
                {"excess": 40, "domains": [_DOMAIN], "max_evictions_ratio": 0.25}
            ),
            qos=0,
            retain=False,
        )
        self.assertEqual(
            self.sample(
                "wgkex_broker_rebalance_hint_excess_peers_total",
                worker="gw1",
                kind="drain",
            ),
            40,
        )

    def test_metrics_endpoint(self):
        """/metrics exports worker state and request counters in the Prometheus format."""
        self.add_worker("gw1", 10)
        self.add_worker("gw2", 30)
        self.status("gw2", b"2")
        ok_before = self.sample(
            "wgkex_broker_key_exchange_requests_total", api="v2", result="ok"
        )
        invalid_before = self.sample(
            "wgkex_broker_key_exchange_requests_total", api="v1", result="invalid"
        )
        assigned_before = self.sample(
            "wgkex_broker_assignments_total", worker="gw1", domain=_DOMAIN
        )
        self.assertEqual(
            self.post("v2", {"domain": _DOMAIN, "public_key": _PUBKEY}).status_code,
            200,
        )
        self.post("v1", {"domain": _DOMAIN, "public_key": "invalid"})

        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.content_type.startswith("text/plain"))
        text = resp.get_data(as_text=True)
        self.assertIn('wgkex_broker_worker_online{worker="gw1"} 1.0', text)
        self.assertIn('wgkex_broker_worker_draining{worker="gw2"} 1.0', text)
        self.assertIn('wgkex_broker_worker_pending_assignments{worker="gw1"} 1.0', text)
        self.assertIn('wgkex_broker_worker_effective_peers{worker="gw1"} 11.0', text)
        # The draining worker has no target, gw1 is the only active worker
        self.assertIn('wgkex_broker_worker_target_peers{worker="gw1"} 11.0', text)
        self.assertIn('wgkex_broker_worker_target_peers{worker="gw2"} 0.0', text)
        self.assertIn(
            f'wgkex_broker_worker_domain_connected_peers{{domain="{_DOMAIN}",worker="gw2"}} 30.0',
            text,
        )
        self.assertEqual(
            self.sample(
                "wgkex_broker_key_exchange_requests_total", api="v2", result="ok"
            ),
            ok_before + 1,
        )
        self.assertEqual(
            self.sample(
                "wgkex_broker_key_exchange_requests_total", api="v1", result="invalid"
            ),
            invalid_before + 1,
        )
        self.assertEqual(
            self.sample("wgkex_broker_assignments_total", worker="gw1", domain=_DOMAIN),
            assigned_before + 1,
        )

    def test_metrics_endpoint_disabled(self):
        with mock.patch.object(config.get_config().metrics, "broker_endpoint", False):
            self.assertEqual(self.client.get("/metrics").status_code, 404)

    def test_invalid_mqtt_payload_counted(self):
        before = self.sample("wgkex_broker_mqtt_message_errors_total", handler="status")
        self.status("gw1", b"garbage")
        self.assertEqual(
            self.sample("wgkex_broker_mqtt_message_errors_total", handler="status"),
            before + 1,
        )


class BrokerRateLimitTest(BrokerTestCase):
    """Rate-limited requests get the normal 200 response, but the key is not published again."""

    def limit(self, per_client: int = 1, global_requests: int = 0) -> None:
        self.app.rate_limiter = self.app.RateLimiter(
            per_client, 60, global_requests, 60
        )

    def test_v1_rate_limited_still_ok(self):
        self.limit()
        body = {"domain": _DOMAIN, "public_key": _PUBKEY}
        self.assertEqual(self.post("v1", body).status_code, 200)
        self.app.mqtt.reset_mock()
        resp = self.post("v1", body)
        self.assertEqual(resp.status_code, 200)
        self.assertDictEqual(resp.get_json(), {"Message": "OK"})
        self.app.mqtt.publish.assert_not_called()

    def test_v2_rate_limited_returns_same_worker(self):
        """A repeated, limited request is answered with the worker the key was sent to."""
        self.limit()
        self.add_worker("gw1", 10)
        self.add_worker("gw2", 10)
        body = {"domain": _DOMAIN, "public_key": _PUBKEY}
        first = self.post("v2", body).get_json()
        self.app.mqtt.reset_mock()
        for _ in range(10):
            resp = self.post("v2", body)
            self.assertEqual(resp.status_code, 200)
            self.assertDictEqual(resp.get_json(), first)
        self.app.mqtt.publish.assert_not_called()
        self.assertEqual(
            self.app.worker_metrics.get_pending_count("gw1")
            + self.app.worker_metrics.get_pending_count("gw2"),
            1,
        )

    def test_v2_rate_limited_new_key_gets_valid_endpoint(self):
        """A limited request for an unknown key still gets a valid endpoint, nothing is published."""
        self.limit(per_client=0, global_requests=1)
        self.add_worker("gw1", 10)
        self.post("v2", {"domain": _DOMAIN, "public_key": _PUBKEY})
        self.app.mqtt.reset_mock()
        resp = self.post("v2", {"domain": _DOMAIN, "public_key": _PUBKEY2})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            set(resp.get_json()["Endpoint"]),
            {"Address", "Port", "PublicKey", "AllowedIPs"},
        )
        self.assertIsInstance(resp.get_json()["Endpoint"]["Port"], str)
        self.app.mqtt.publish.assert_not_called()
        self.assertEqual(self.app.worker_metrics.get_pending_count("gw1"), 1)

    def test_rate_limited_metric(self):
        self.limit()
        before = self.sample(
            "wgkex_broker_key_exchange_requests_total", api="v1", result="rate_limited"
        )
        body = {"domain": _DOMAIN, "public_key": _PUBKEY}
        self.post("v1", body)
        self.post("v1", body)
        self.assertEqual(
            self.sample(
                "wgkex_broker_key_exchange_requests_total",
                api="v1",
                result="rate_limited",
            ),
            before + 1,
        )

    def post_from(self, version: str, forwarded_for: str, remote: str = "127.0.0.1"):
        return self.client.post(
            f"/api/{version}/wg/key/exchange",
            data=json.dumps({"domain": _DOMAIN, "public_key": _PUBKEY}),
            headers={"X-Forwarded-For": forwarded_for},
            environ_base={"REMOTE_ADDR": remote},
        )

    def test_forwarded_for_with_trusted_proxy(self):
        """Behind a trusted proxy, clients are limited by their own address."""
        self.limit()
        for trusted in (1, [ipaddress.ip_network("127.0.0.0/8")]):
            with mock.patch.object(
                config.get_config().broker, "trusted_proxies", trusted
            ):
                for client in ("192.0.2.1", "192.0.2.2"):
                    self.app.mqtt.reset_mock()
                    self.post_from("v1", f"10.9.9.9, {client}")
                    self.app.mqtt.publish.assert_called_once()
            self.limit()

    def test_forwarded_for_spoofed_from_untrusted_peer(self):
        """A spoofed header from a peer that isn't a trusted proxy doesn't evade the limit."""
        self.limit()
        trusted = [ipaddress.ip_network("10.0.0.0/8")]
        with mock.patch.object(config.get_config().broker, "trusted_proxies", trusted):
            self.post_from("v1", "192.0.2.1", remote="198.51.100.7")
            self.app.mqtt.reset_mock()
            resp = self.post_from("v1", "192.0.2.2", remote="198.51.100.7")
        self.assertEqual(resp.status_code, 200)
        self.app.mqtt.publish.assert_not_called()

    def test_log_client_ip(self):
        self.add_worker("gw1", 10)
        broker_cfg = config.get_config().broker
        with mock.patch.object(broker_cfg, "trusted_proxies", 1):
            for version in ("v1", "v2"):
                with self.assertLogs(level="INFO") as logs:
                    self.post_from(version, "192.0.2.1")
                line = next(m for m in logs.output if "_key_exchange: Domain" in m)
                self.assertNotIn("Client:", line)

                with (
                    mock.patch.object(broker_cfg, "log_client_ip", True),
                    self.assertLogs(level="INFO") as logs,
                ):
                    resp = self.post_from(version, "spoofed, 192.0.2.1")
                line = next(m for m in logs.output if "_key_exchange: Domain" in m)
                self.assertIn(f"Domain: {_DOMAIN}, Key:{_PUBKEY}", line)
                self.assertTrue(line.endswith("Client: 192.0.2.1"), line)
                self.assertEqual(resp.status_code, 200)


class BrokerBlocklistTest(BrokerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.app.blocklist = self.app.KeyList("blocklist", keys=[_PUBKEY])

    def test_blocked_key_rejected(self):
        self.add_worker("gw1", 10)
        for version in ("v1", "v2"):
            resp = self.post(version, {"domain": _DOMAIN, "public_key": _PUBKEY})
            self.assertEqual(resp.status_code, 403)
            self.assertEqual(resp.get_json(), {"error": {"message": "key is blocked"}})
        self.app.mqtt.publish.assert_not_called()
        self.assertEqual(self.app.worker_metrics.get_pending_count("gw1"), 0)

    def test_other_keys_allowed(self):
        resp = self.post("v1", {"domain": _DOMAIN, "public_key": _PUBKEY2})
        self.assertEqual(resp.status_code, 200)

    def test_newly_blocked_keys_removed_from_workers(self):
        self.add_worker("gw1", 10)
        self.add_worker("gw2", 10)
        self.app._on_blocklist_change({_PUBKEY2}, set())
        self.app.mqtt.publish.assert_has_calls(
            [
                mock.call(f"wireguard-worker/gw1/{_DOMAIN}/remove", _PUBKEY2, qos=1),
                mock.call(f"wireguard-worker/gw2/{_DOMAIN}/remove", _PUBKEY2, qos=1),
            ]
        )


class BrokerLocationTest(BrokerTestCase):
    def test_location_parsing(self):
        base = {"domain": _DOMAIN, "public_key": _PUBKEY}
        self.assertEqual(
            self.app.KeyExchange.from_dict({**base, "location": "muc"}).location, "muc"
        )
        for invalid in (None, 5, "", "a b", "x" * 65, ["muc"]):
            self.assertIsNone(
                self.app.KeyExchange.from_dict({**base, "location": invalid}).location
            )

    def test_location_preferred(self):
        workers = config.Workers.from_dict(
            {
                "gw1": {"weight": 1, "location": "muc"},
                "gw2": {"weight": 1, "location": "fra"},
            }
        )
        self.add_worker("gw1", 100)
        self.add_worker("gw2", 100)
        with mock.patch.object(config.get_config(), "workers", workers):
            for _ in range(5):
                resp = self.post(
                    "v2", {"domain": _DOMAIN, "public_key": _PUBKEY, "location": "fra"}
                )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(
                    resp.get_json()["Endpoint"]["Address"], "gw2.ext.example.net"
                )

    def test_unknown_location_still_answers(self):
        self.add_worker("gw1", 10)
        resp = self.post(
            "v2", {"domain": _DOMAIN, "public_key": _PUBKEY, "location": "nowhere"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["Endpoint"]["Address"], "gw1.ext.example.net")


class BrokerSharedAssignmentsTest(BrokerTestCase):
    def assignment(self, broker: str, payload: bytes) -> None:
        m = mock.MagicMock()
        m.topic = f"wireguard-broker/{broker}/assignment"
        m.payload = payload
        self.app.handle_mqtt_message_assignment(None, None, m)

    def test_disabled_by_default(self):
        self.add_worker("gw1", 10)
        self.post("v2", {"domain": _DOMAIN, "public_key": _PUBKEY})
        topics = [c.args[0] for c in self.app.mqtt.publish.call_args_list]
        self.assertFalse(any(t.startswith("wireguard-broker/") for t in topics))
        self.assignment(
            "other",
            json.dumps({"worker": "gw1", "domain": _DOMAIN, "key": _PUBKEY2}).encode(),
        )
        self.assertEqual(self.app.worker_metrics.get_pending_count("gw1"), 1)

    def test_shared_assignments(self):
        self.add_worker("gw1", 10)
        lb = config.get_config().load_balancing
        with mock.patch.object(lb, "share_pending_assignments", True):
            self.post("v2", {"domain": _DOMAIN, "public_key": _PUBKEY})
            self.app.mqtt.publish.assert_any_call(
                f"wireguard-broker/{self.app.broker_id}/assignment",
                json.dumps({"worker": "gw1", "domain": _DOMAIN, "key": _PUBKEY}),
                qos=0,
                retain=False,
            )
            msg = json.dumps({"worker": "gw1", "domain": _DOMAIN, "key": _PUBKEY2})
            # Own messages are ignored
            self.assignment(self.app.broker_id, msg.encode())
            self.assertEqual(self.app.worker_metrics.get_pending_count("gw1"), 1)
            self.assignment("other", msg.encode())
            self.assertEqual(self.app.worker_metrics.get_pending_count("gw1"), 2)
            self.assertEqual(self.app.assignment_history.get(_PUBKEY2).worker, "gw1")
            # Invalid payloads don't raise
            self.assignment("other", b"garbage")
            self.assignment("other", b'{"worker": "gw1"}')
            self.assignment(
                "other",
                json.dumps({"worker": "gw1", "domain": _DOMAIN, "key": "x"}).encode(),
            )
            self.assertEqual(self.app.worker_metrics.get_pending_count("gw1"), 2)


class MqttConnectTestCase(unittest.TestCase):
    """Regression test for subscriptions being lost when the CONNACK arrives early."""

    _EXPECTED_TOPICS = (
        "wireguard-metrics/#",
        "wireguard-worker/+/status",
        "wireguard-worker/+/+/data",
        "wireguard-broker/+/assignment",
    )

    @classmethod
    def setUpClass(cls) -> None:
        config._parsed_config = config.Config.from_dict(
            {
                "domains": [_DOMAIN],
                "domain_prefixes": ["ffmuc_"],
                "workers": {},
                "mqtt": {"broker_url": "", "username": "", "password": ""},
                "load_balancing": {"share_pending_assignments": True},
            }
        )
        mqtt_mock = mock.MagicMock()
        cls.mqtt_mock = mqtt_mock
        cls.connect_handler = None
        cls.subscribed_on_connect: list[str] = []

        def on_connect():
            def decorator(f):
                cls.connect_handler = f
                return f

            return decorator

        def init_app(app):
            # Deliver the CONNACK immediately, as a fast broker would.
            if cls.connect_handler is not None:
                cls.connect_handler(None, None, {}, 0)
            cls.subscribed_on_connect = [
                c.args[0] for c in mqtt_mock.subscribe.call_args_list
            ]

        mqtt_mock.on_connect.side_effect = on_connect
        mqtt_mock.on_topic.side_effect = _decorator_factory
        mqtt_mock.on_message.side_effect = _decorator_factory
        mqtt_mock.init_app.side_effect = init_app
        cls.mqtt_patcher = mock.patch("flask_mqtt.Mqtt", return_value=mqtt_mock)
        mqtt_class = cls.mqtt_patcher.start()
        sys.modules.pop("wgkex.broker.app", None)
        cls.app = importlib.import_module("wgkex.broker.app")
        cls.mqtt_class = mqtt_class

    @classmethod
    def tearDownClass(cls) -> None:
        cls.mqtt_patcher.stop()
        sys.modules.pop("wgkex.broker.app", None)
        config._parsed_config = None

    def test_connects_after_handlers_are_registered(self):
        self.mqtt_class.assert_called_once_with()
        self.mqtt_mock.init_app.assert_called_once_with(self.app.app)
        names = [c[0] for c in self.mqtt_mock.mock_calls]
        init_index = names.index("init_app")
        for decorator in ("on_connect", "on_topic", "on_message"):
            self.assertIn(decorator, names)
            last = len(names) - 1 - names[::-1].index(decorator)
            self.assertLess(last, init_index, decorator)

    def test_subscribes_on_immediate_connect(self):
        self.assertEqual(self.subscribed_on_connect, list(self._EXPECTED_TOPICS))

    def test_resubscribes_on_reconnect(self):
        self.mqtt_mock.subscribe.reset_mock()
        self.app.handle_mqtt_connect(None, None, {}, 0)
        self.assertEqual(
            [c.args[0] for c in self.mqtt_mock.subscribe.call_args_list],
            list(self._EXPECTED_TOPICS),
        )


if __name__ == "__main__":
    unittest.main()
