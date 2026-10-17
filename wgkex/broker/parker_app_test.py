import importlib
import ipaddress
import json
import sys
import unittest
from unittest import mock

import flask_mqtt
import paho.mqtt.client
import paho.mqtt.enums

from wgkex.config import config

_DOMAIN = "_test_domain"
_PUBLIC_KEY = "TszFS3oFRdhsJP3K0VOlklGMGYZy+oFCtlaghXJqW2g="


class MqttStub:
    def __init__(self, *args, **kwargs):
        pass

    def init_app(self, app, **kwargs):
        return None

    def on_topic(self, topic=None):
        return lambda func: func

    def on_connect(self):
        return lambda func: func

    def on_message(self):
        return lambda func: func

    def subscribe(self, *args, **kwargs):
        return None

    def publish(self, *args, **kwargs):
        return None


def _test_config() -> config.Config:
    return config.Config.from_dict(
        {
            "domains": [_DOMAIN],
            "domain_prefixes": ["_test_"],
            "mqtt": {"broker_url": "", "username": "", "password": ""},
        }
    )


def _parker_config() -> config.Config:
    return config.Config.from_dict(
        {
            "parker": {
                "enabled": True,
                "464xlat": True,
                "ipam": "json",
                "prefixes": {
                    "ipv4": {"clat_subnet": "10.80.96.0/22"},
                    "ipv6": {"length": 63},
                },
            },
            "broker_signing_key": "signing-key",
            "domains": [],
            "domain_prefixes": [],
            "workers": {},
            "mqtt": {"broker_url": "", "username": "", "password": ""},
        }
    )


def _message(topic: str, payload: bytes) -> paho.mqtt.client.MQTTMessage:
    message = paho.mqtt.client.MQTTMessage()
    message.topic = topic.encode()
    message.payload = payload
    return message


class TestBrokerApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        config._parsed_config = _test_config()
        with mock.patch.object(flask_mqtt, "Mqtt", MqttStub):
            cls.broker_app = importlib.import_module("wgkex.broker.app")

    @classmethod
    def tearDownClass(cls) -> None:
        config._parsed_config = None
        sys.modules.pop("wgkex.broker.app", None)
        sys.modules.pop("wgkex.broker.signer", None)

    def setUp(self) -> None:
        config._parsed_config = _test_config()
        self.broker_app.worker_data.clear()
        self.broker_app.parker_worker_data.clear()
        self.broker_app.worker_metrics.data.clear()
        self.broker_app.parker_worker_metrics.data.clear()
        self.broker_app.parker_worker_metrics._pending.clear()
        self.broker_app.rate_limiter = self.broker_app._create_rate_limiter()
        self.broker_app.blocklist = self.broker_app._create_blocklist()

    def test_load_parker_ipam_modes(self):
        self.assertIsNone(self.broker_app._load_parker_ipam())

        cfg = mock.MagicMock()
        cfg.parker.enabled = True
        cfg.parker.ipam = config.Parker.IPAM.JSON
        with (
            mock.patch.object(self.broker_app.config, "get_config", return_value=cfg),
            mock.patch.object(
                self.broker_app, "JSONFileIPAM", return_value="json-ipam"
            ),
        ):
            self.assertEqual(self.broker_app._load_parker_ipam(), "json-ipam")

        cfg.parker.ipam = config.Parker.IPAM.NETBOX
        cfg.parker.xlat = False
        cfg.netbox = config.Netbox(url="https://netbox", api_key="token")
        with (
            mock.patch.object(self.broker_app.config, "get_config", return_value=cfg),
            mock.patch.object(
                self.broker_app, "NetboxIPAM", return_value="netbox-ipam"
            ) as netbox,
        ):
            self.assertEqual(self.broker_app._load_parker_ipam(), "netbox-ipam")
        netbox.assert_called_once_with(
            api_url="https://netbox", token="token", xlat=False
        )

        cfg.netbox = None
        with (
            mock.patch.object(self.broker_app.config, "get_config", return_value=cfg),
            self.assertRaisesRegex(ValueError, "no netbox config"),
        ):
            self.broker_app._load_parker_ipam()

        cfg.parker.ipam = "unsupported"
        with (
            mock.patch.object(self.broker_app.config, "get_config", return_value=cfg),
            self.assertRaises(NotImplementedError),
        ):
            self.broker_app._load_parker_ipam()

    def test_mqtt_connect_success_and_failure(self):
        mqtt = mock.MagicMock()
        client = mock.MagicMock(host="mqtt", port=1883)
        with (
            mock.patch.object(
                self.broker_app.config, "get_config", return_value=_parker_config()
            ),
            mock.patch.object(self.broker_app, "mqtt", mqtt),
        ):
            self.broker_app.handle_mqtt_connect(
                client, b"", {}, paho.mqtt.enums.ConnackCode.CONNACK_ACCEPTED
            )
        subscribed = [c.args[0] for c in mqtt.subscribe.call_args_list]
        # Legacy topics stay subscribed, so mixed fleets keep working
        self.assertIn("wireguard-metrics/#", subscribed)
        self.assertIn("parker/wireguard-worker/+/status", subscribed)
        self.assertIn("parker/wireguard-worker/+/data", subscribed)
        self.assertIn("parker/wireguard-metrics/+/connected_peers", subscribed)
        mqtt.publish.assert_not_called()

        mqtt.reset_mock()
        with mock.patch.object(self.broker_app, "mqtt", mqtt):
            self.broker_app.handle_mqtt_connect(client, b"", {}, 1)
        mqtt.subscribe.assert_not_called()
        mqtt.publish.assert_not_called()

    def test_worker_metric_status_and_data_handlers(self):
        self.broker_app.handle_mqtt_message_metrics(
            None,
            b"",
            _message(f"wireguard-metrics/{_DOMAIN}/worker/connected_peers", b"4"),
        )
        self.assertEqual(
            self.broker_app.worker_metrics.get("worker")
            .get_domain_metrics(_DOMAIN)
            .get("connected_peers"),
            4,
        )
        self.broker_app.handle_mqtt_message_metrics(
            None,
            b"",
            _message("wireguard-metrics/unknown/worker/connected_peers", b"4"),
        )
        self.broker_app.handle_mqtt_message_metrics(
            None,
            b"",
            _message(f"wireguard-metrics/{_DOMAIN}//connected_peers", b"4"),
        )

        online_metrics = mock.MagicMock()
        online_metrics.is_online.return_value = True
        with (
            mock.patch.object(
                self.broker_app.worker_metrics, "get", return_value=online_metrics
            ),
            mock.patch.object(self.broker_app.worker_metrics, "set_offline") as offline,
        ):
            self.broker_app.handle_mqtt_message_status(
                None, b"", _message("wireguard-worker/worker/status", b"0")
            )
        offline.assert_called_once_with("worker")

        offline_metrics = mock.MagicMock()
        offline_metrics.is_online.return_value = False
        with (
            mock.patch.object(
                self.broker_app.worker_metrics, "get", return_value=offline_metrics
            ),
            mock.patch.object(self.broker_app.worker_metrics, "set_online") as online,
        ):
            self.broker_app.handle_mqtt_message_status(
                None, b"", _message("wireguard-worker/worker/status", b"1")
            )
        online.assert_called_once_with("worker")

        self.broker_app.handle_mqtt_message_data(
            None,
            b"",
            _message(
                f"wireguard-worker/worker/{_DOMAIN}/data",
                json.dumps({"Port": 51820}).encode(),
            ),
        )
        self.assertEqual(
            self.broker_app.worker_data[("worker", _DOMAIN)], {"Port": 51820}
        )
        self.broker_app.handle_mqtt_message_data(
            None,
            b"",
            _message("wireguard-worker/worker/unknown/data", b"{}"),
        )
        self.broker_app.handle_mqtt_message_data(
            None,
            b"",
            _message(f"wireguard-worker/worker/{_DOMAIN}/data", b"[]"),
        )

    def test_parker_exchange_reports_query_and_ipam_failures(self):
        cfg = _parker_config()
        ipam = mock.MagicMock()
        query = {"pubkey": _PUBLIC_KEY, "nonce": "nonce"}

        with (
            mock.patch.object(self.broker_app.config, "get_config", return_value=cfg),
            mock.patch.object(self.broker_app, "ipam", ipam),
        ):
            response = self.broker_app.app.test_client().get(
                "/config", query_string={"pubkey": "invalid"}
            )
            self.assertEqual(response.status_code, 400)

            ipam.get_or_allocate_prefix.side_effect = RuntimeError("allocation failed")
            response = self.broker_app.app.test_client().get(
                "/config", query_string=query
            )
            self.assertEqual(response.status_code, 500)

            ipam.get_or_allocate_prefix.side_effect = None
            ipam.get_or_allocate_prefix.return_value = (None, None, [])
            response = self.broker_app.app.test_client().get(
                "/config", query_string=query
            )
            self.assertEqual(response.status_code, 500)

    def test_parker_exchange_rejects_blocked_and_rate_limited_keys(self):
        cfg = _parker_config()
        ipam = mock.MagicMock()
        query = {"pubkey": _PUBLIC_KEY, "nonce": "nonce"}
        requests = self.broker_app.KEY_EXCHANGE_REQUESTS

        with (
            mock.patch.object(self.broker_app.config, "get_config", return_value=cfg),
            mock.patch.object(self.broker_app, "ipam", ipam),
            mock.patch.object(self.broker_app, "mqtt") as mqtt,
        ):
            blocked = requests.labels("v3", "blocked")._value.get()
            self.broker_app.blocklist = self.broker_app.KeyList(
                "blocklist", keys=[_PUBLIC_KEY]
            )
            response = self.broker_app.app.test_client().get(
                "/config", query_string=query
            )
            self.assertEqual(response.status_code, 403)
            self.assertEqual(requests.labels("v3", "blocked")._value.get(), blocked + 1)

            limited = requests.labels("v3", "rate_limited")._value.get()
            self.broker_app.blocklist = self.broker_app.KeyList("blocklist")
            self.broker_app.rate_limiter = self.broker_app.RateLimiter(
                per_client_requests=1,
                per_client_period=60,
                global_requests=0,
                global_period=60,
            )
            self.broker_app.rate_limiter.allow("127.0.0.1")
            response = self.broker_app.app.test_client().get(
                "/config", query_string=query
            )
            self.assertEqual(response.status_code, 429)
            self.assertEqual(
                requests.labels("v3", "rate_limited")._value.get(), limited + 1
            )

        ipam.get_or_allocate_prefix.assert_not_called()
        mqtt.publish.assert_not_called()

    def test_parker_exchange_records_pending_assignments(self):
        cfg = _parker_config()
        cfg.workers = config.Workers.from_dict({"worker1": {"id": 1}}, 10)
        ipam = mock.MagicMock()
        ipam.get_or_allocate_prefix.return_value = (
            None,
            ipaddress.IPv6Network("2001:db8:42::/63"),
            [],
        )
        query = {"pubkey": _PUBLIC_KEY, "nonce": "nonce"}
        self.broker_app.parker_worker_data["worker1"] = {
            "LinkAddress": "fe80::1",
            "ExternalAddress": "worker1.example",
            "Port": 10000,
            "PublicKey": "worker1-key",
        }
        self.broker_app.parker_worker_metrics.update(
            "worker1", "parker", "connected_peers", 3
        )
        self.broker_app.parker_worker_metrics.set_online("worker1")
        ok = self.broker_app.KEY_EXCHANGE_REQUESTS.labels("v3", "ok")._value.get()

        with (
            mock.patch.object(self.broker_app.config, "get_config", return_value=cfg),
            mock.patch.object(self.broker_app, "ipam", ipam),
            mock.patch.object(self.broker_app, "mqtt"),
            mock.patch.object(
                self.broker_app, "sign_response", return_value=b"signature"
            ),
        ):
            response = self.broker_app.app.test_client().get(
                "/config", query_string=query
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                self.broker_app.parker_worker_metrics.get_effective_peer_count(
                    "worker1"
                ),
                4,
            )

            # A periodic poll keeping the same concentrator does not count again
            ipam.get_or_allocate_prefix.return_value = (
                None,
                ipaddress.IPv6Network("2001:db8:42::/63"),
                ["worker1"],
            )
            response = self.broker_app.app.test_client().get(
                "/config", query_string=query
            )
            self.assertEqual(response.status_code, 200)

        self.assertEqual(
            self.broker_app.parker_worker_metrics.get_effective_peer_count("worker1"),
            4,
        )
        self.assertEqual(
            self.broker_app.KEY_EXCHANGE_REQUESTS.labels("v3", "ok")._value.get(),
            ok + 2,
        )

    def test_parker_status_handler_tracks_draining(self):
        self.broker_app.handle_mqtt_message_parker_status(
            None, b"", _message("parker/wireguard-worker/worker/status", b"2")
        )
        self.assertTrue(self.broker_app.parker_worker_metrics.get("worker").online)
        self.assertTrue(self.broker_app.parker_worker_metrics.is_draining("worker"))

        self.broker_app.handle_mqtt_message_parker_status(
            None, b"", _message("parker/wireguard-worker/worker/status", b"1")
        )
        self.assertFalse(self.broker_app.parker_worker_metrics.is_draining("worker"))

        self.broker_app.handle_mqtt_message_parker_status(
            None, b"", _message("parker/wireguard-worker/worker/status", b"0")
        )
        self.assertFalse(self.broker_app.parker_worker_metrics.get("worker").online)

    def test_parker_exchange_worker_fallback_and_error_paths(self):
        cfg = _parker_config()
        ipam = mock.MagicMock()
        ipam.get_or_allocate_prefix.return_value = (
            None,
            ipaddress.IPv6Network("2001:db8:42::/63"),
            [],
        )
        query = {"pubkey": _PUBLIC_KEY, "nonce": "nonce"}
        worker = self.broker_app.WorkerResult(
            name="worker", id=7, diff=0, peers=0, target=0
        )

        with (
            mock.patch.object(self.broker_app.config, "get_config", return_value=cfg),
            mock.patch.object(self.broker_app, "ipam", ipam),
            mock.patch.object(
                self.broker_app.parker_worker_metrics,
                "get_best_workers",
                return_value=[],
            ),
        ):
            response = self.broker_app.app.test_client().get(
                "/config", query_string=query
            )
            self.assertEqual(response.status_code, 400)
            ipam.update_prefix.assert_not_called()

            self.broker_app.parker_worker_data["worker"] = {
                "LinkAddress": "fe80::1",
                "ExternalAddress": "gateway.example",
                "Port": 51820,
                "PublicKey": "gateway-key",
            }
            response = self.broker_app.app.test_client().get(
                "/config", query_string=query
            )
            self.assertEqual(response.status_code, 400)
            ipam.update_prefix.assert_not_called()

            cfg.workers = config.Workers.from_dict({"worker": {"id": 7}}, 10)
            response = self.broker_app.app.test_client().get(
                "/config", query_string=query
            )
            self.assertEqual(response.status_code, 400)
            ipam.update_prefix.assert_not_called()

            self.broker_app.parker_worker_metrics.set_online("worker")
            with (
                mock.patch.object(self.broker_app, "mqtt") as mqtt,
                mock.patch.object(
                    self.broker_app, "sign_response", return_value=b"signature"
                ),
            ):
                response = self.broker_app.app.test_client().get(
                    "/config", query_string=query
                )
            self.assertEqual(response.status_code, 200)
            payload = json.loads(response.data.split(b"\n", 1)[0])
            self.assertEqual(payload["concentrators"][0]["id"], 7)
            ipam.update_prefix.assert_called_once_with(
                _PUBLIC_KEY, False, True, ["worker"]
            )
            self.assertEqual(
                [call.args[0] for call in mqtt.publish.call_args_list],
                ["parker/wireguard/worker"],
            )

            self.broker_app.parker_worker_data.clear()
            ipam.update_prefix.reset_mock()
            with mock.patch.object(
                self.broker_app.parker_worker_metrics,
                "get_best_workers",
                return_value=[worker],
            ):
                response = self.broker_app.app.test_client().get(
                    "/config", query_string=query
                )
            self.assertEqual(response.status_code, 500)
            ipam.update_prefix.assert_not_called()

            self.broker_app.parker_worker_data["worker"] = {
                "LinkAddress": "fe80::1",
                "ExternalAddress": "gateway.example",
                "Port": 51820,
                "PublicKey": "gateway-key",
            }
            cfg.broker_signing_key = None
            with mock.patch.object(
                self.broker_app.parker_worker_metrics,
                "get_best_workers",
                return_value=[worker],
            ):
                response = self.broker_app.app.test_client().get(
                    "/config", query_string=query
                )
            self.assertEqual(response.status_code, 500)

    def test_parker_exchange_endpoint_uses_configured_endpoint_name(self):
        cfg = _parker_config()
        ipam = mock.MagicMock()
        ipam.get_or_allocate_prefix.return_value = (
            None,
            ipaddress.IPv6Network("2001:db8:42::/63"),
            [],
        )
        query = {"pubkey": _PUBLIC_KEY, "nonce": "nonce"}
        self.broker_app.parker_worker_data["worker"] = {
            "LinkAddress": "fe80::1",
            "ExternalAddress": "198.51.100.1",
            "Port": 10000,
            "PublicKey": "gateway-key",
        }
        self.broker_app.parker_worker_metrics.set_online("worker")

        def _endpoint() -> str:
            with (
                mock.patch.object(
                    self.broker_app.config, "get_config", return_value=cfg
                ),
                mock.patch.object(self.broker_app, "ipam", ipam),
                mock.patch.object(
                    self.broker_app, "sign_response", return_value=b"signature"
                ),
            ):
                response = self.broker_app.app.test_client().get(
                    "/config", query_string=query
                )
            self.assertEqual(response.status_code, 200)
            payload = json.loads(response.data.split(b"\n", 1)[0])
            return payload["concentrators"][0]["endpoint"]

        cfg.workers = config.Workers.from_dict({"worker": {"id": 7}}, 10)
        self.assertEqual(_endpoint(), "198.51.100.1:10000")

        cfg.workers = config.Workers.from_dict(
            {"worker": {"id": 7, "endpoint_name": "gw1.lab"}}, 10
        )
        self.assertEqual(_endpoint(), "gw1.lab:10000")

    def test_parker_exchange_publishes_peer_only_to_selected_workers(self):
        cfg = _parker_config()
        cfg.workers = config.Workers.from_dict(
            {"worker1": {"id": 1}, "worker2": {"id": 2}}, 10
        )
        ipam = mock.MagicMock()
        ipam.get_or_allocate_prefix.return_value = (
            None,
            ipaddress.IPv6Network("2001:db8:42::/63"),
            [],
        )
        query = {"pubkey": _PUBLIC_KEY, "nonce": "nonce"}
        for name in ("worker1", "worker2"):
            self.broker_app.parker_worker_data[name] = {
                "LinkAddress": "fe80::1",
                "ExternalAddress": f"{name}.example",
                "Port": 10000,
                "PublicKey": f"{name}-key",
            }
        workers = [
            self.broker_app.WorkerResult(
                name="worker1", id=1, diff=0, peers=0, target=0
            ),
            self.broker_app.WorkerResult(
                name="worker2", id=2, diff=0, peers=0, target=0
            ),
        ]

        with (
            mock.patch.object(self.broker_app.config, "get_config", return_value=cfg),
            mock.patch.object(self.broker_app, "ipam", ipam),
            mock.patch.object(self.broker_app, "mqtt") as mqtt,
            mock.patch.object(
                self.broker_app.parker_worker_metrics,
                "get_best_workers",
                return_value=workers,
            ),
            mock.patch.object(
                self.broker_app, "sign_response", return_value=b"signature"
            ),
        ):
            response = self.broker_app.app.test_client().get(
                "/config", query_string=query
            )

        self.assertEqual(response.status_code, 200)
        published = [
            (call.args[0], json.loads(call.args[1]))
            for call in mqtt.publish.call_args_list
        ]
        self.assertEqual(
            [topic for topic, _ in published],
            ["parker/wireguard/worker1", "parker/wireguard/worker2"],
        )
        for _, payload in published:
            self.assertEqual(payload["PublicKey"], _PUBLIC_KEY)
            self.assertEqual(payload["Range6"], "2001:db8:42::/63")

    def test_parker_exchange_removes_peer_from_deselected_workers(self):
        cfg = _parker_config()
        cfg.workers = config.Workers.from_dict(
            {"worker1": {"id": 1}, "worker2": {"id": 2}}, 10
        )
        ipam = mock.MagicMock()
        ipam.get_or_allocate_prefix.return_value = (
            None,
            ipaddress.IPv6Network("2001:db8:42::/63"),
            ["worker1", "worker3"],
        )
        query = {"pubkey": _PUBLIC_KEY, "nonce": "nonce"}
        for name in ("worker1", "worker2"):
            self.broker_app.parker_worker_data[name] = {
                "LinkAddress": "fe80::1",
                "ExternalAddress": f"{name}.example",
                "Port": 10000,
                "PublicKey": f"{name}-key",
            }
        workers = [
            self.broker_app.WorkerResult(
                name="worker1", id=1, diff=0, peers=0, target=0
            ),
            self.broker_app.WorkerResult(
                name="worker2", id=2, diff=0, peers=0, target=0
            ),
        ]

        with (
            mock.patch.object(self.broker_app.config, "get_config", return_value=cfg),
            mock.patch.object(self.broker_app, "ipam", ipam),
            mock.patch.object(self.broker_app, "mqtt") as mqtt,
            mock.patch.object(
                self.broker_app.parker_worker_metrics,
                "get_best_workers",
                return_value=workers,
            ),
            mock.patch.object(
                self.broker_app, "sign_response", return_value=b"signature"
            ),
        ):
            response = self.broker_app.app.test_client().get(
                "/config", query_string=query
            )

        self.assertEqual(response.status_code, 200)
        published = {
            call.args[0]: json.loads(call.args[1])
            for call in mqtt.publish.call_args_list
        }
        self.assertEqual(
            sorted(published),
            [
                "parker/wireguard/worker1",
                "parker/wireguard/worker2",
                "parker/wireguard/worker3",
            ],
        )
        self.assertNotIn("Remove", published["parker/wireguard/worker1"])
        self.assertNotIn("Remove", published["parker/wireguard/worker2"])
        self.assertIs(published["parker/wireguard/worker3"]["Remove"], True)
        self.assertEqual(
            published["parker/wireguard/worker3"]["PublicKey"], _PUBLIC_KEY
        )
        self.assertEqual(
            published["parker/wireguard/worker3"]["Range6"], "2001:db8:42::/63"
        )

    def test_parker_metric_status_and_invalid_data_handlers(self):
        self.broker_app.handle_mqtt_message_parker_metrics(
            None,
            b"",
            _message("parker/wireguard-metrics//connected_peers", b"4"),
        )
        self.assertEqual(self.broker_app.parker_worker_metrics.data, {})
        self.broker_app.handle_mqtt_message_parker_metrics(
            None,
            b"",
            _message("parker/wireguard-metrics/worker/connected_peers", b"4"),
        )
        self.assertEqual(
            self.broker_app.parker_worker_metrics.get("worker")
            .get_domain_metrics("parker")
            .get("connected_peers"),
            4,
        )

        online_metrics = mock.MagicMock()
        online_metrics.is_online.return_value = True
        with (
            mock.patch.object(
                self.broker_app.parker_worker_metrics,
                "get",
                return_value=online_metrics,
            ),
            mock.patch.object(
                self.broker_app.parker_worker_metrics, "set_offline"
            ) as offline,
        ):
            self.broker_app.handle_mqtt_message_parker_status(
                None,
                b"",
                _message("parker/wireguard-worker/worker/status", b"0"),
            )
        offline.assert_called_once_with("worker")

        offline_metrics = mock.MagicMock()
        offline_metrics.is_online.return_value = False
        with (
            mock.patch.object(
                self.broker_app.parker_worker_metrics,
                "get",
                return_value=offline_metrics,
            ),
            mock.patch.object(
                self.broker_app.parker_worker_metrics, "set_online"
            ) as online,
        ):
            self.broker_app.handle_mqtt_message_parker_status(
                None,
                b"",
                _message("parker/wireguard-worker/worker/status", b"1"),
            )
        online.assert_called_once_with("worker")

        self.broker_app.handle_mqtt_message_parker_data(
            None,
            b"",
            _message("parker/wireguard-worker/worker/data", b"[]"),
        )
        self.assertNotIn("worker", self.broker_app.parker_worker_data)

    def test_metric_and_status_handlers_ignore_invalid_payloads(self):
        self.broker_app.handle_mqtt_message_metrics(
            None,
            b"",
            _message(f"wireguard-metrics/{_DOMAIN}/worker/connected_peers", b""),
        )
        self.assertNotIn("worker", self.broker_app.worker_metrics.data)
        self.broker_app.handle_mqtt_message_parker_metrics(
            None,
            b"",
            _message("parker/wireguard-metrics/worker/connected_peers", b"invalid"),
        )
        self.assertNotIn("worker", self.broker_app.parker_worker_metrics.data)

        with mock.patch.object(self.broker_app.worker_metrics, "get") as get:
            self.broker_app.handle_mqtt_message_status(
                None, b"", _message("wireguard-worker/worker/status", b"")
            )
        get.assert_not_called()
        with mock.patch.object(self.broker_app.parker_worker_metrics, "get") as get:
            self.broker_app.handle_mqtt_message_parker_status(
                None, b"", _message("parker/wireguard-worker/worker/status", b"invalid")
            )
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
