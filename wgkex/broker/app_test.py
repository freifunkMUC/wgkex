"""Tests for the broker HTTP API, especially its compatibility with the Gluon client.

The Gluon package ffmuc-mesh-vpn-wireguard-vxlan relies on the exact response shapes asserted here.
"""

import json
import sys
import unittest
from unittest import mock

from wgkex.common.mqtt import CONNECTED_PEERS_METRIC
from wgkex.config import config

_DOMAIN = "ffmuc_welt"
_PUBKEY = "TszFS3oFRdhsJP3K0VOlklGMGYZy+oFCtlaghXJqW2g="


def _decorator_factory(*args, **kwargs):
    return lambda f: f


class BrokerAppTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
