import importlib
import ipaddress
import json
import sys
import unittest
from unittest import mock

import flask_mqtt
import paho.mqtt.client
import paho.mqtt.enums

import wgkex.broker
from wgkex.config import config

_DOMAIN = "_test_domain"
_PUBLIC_KEY = "TszFS3oFRdhsJP3K0VOlklGMGYZy+oFCtlaghXJqW2g="


def _evict_broker_modules(*names: str) -> None:
    """Drops imported broker modules from sys.modules and from the wgkex.broker package.

    `from wgkex.broker import app` prefers the package attribute over sys.modules, so
    popping sys.modules alone would hand a stale, stubbed module to later tests.
    """
    for name in names:
        sys.modules.pop(f"wgkex.broker.{name}", None)
        if hasattr(wgkex.broker, name):
            delattr(wgkex.broker, name)


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

    def on_subscribe(self):
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


def _multisite_config(vie_capacity=None, vie_weight=1) -> config.Config:
    cfg = _parker_config()
    cfg.parker = config.Parker.from_dict(
        {
            "enabled": True,
            "464xlat": True,
            "ipam": "json",
            "prefixes": {
                "ipv4": {"clat_subnet": "10.80.96.0/22"},
                "ipv6": {"length": 63},
            },
            "sites": {
                "muc": {"tunnel_id": 101, "dns64": "2001:db8:1::64", "json_file": "m"},
                "vie": {
                    "tunnel_id": 102,
                    "dns64": "2001:db8:2::64",
                    "json_file": "v",
                    "capacity": vie_capacity,
                    "weight": vie_weight,
                },
            },
        }
    )
    cfg.workers = config.Workers.from_dict(
        {
            "muc1": {"id": 1, "pop": "muc"},
            "muc2": {"id": 2, "pop": "muc"},
            "vie1": {"id": 3, "pop": "vie"},
        },
        10,
    )
    return cfg


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
        _evict_broker_modules("app", "signer")

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
        cfg.parker.sites = {}
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
            mock.patch.object(self.broker_app, "IPAMCache") as cache,
            mock.patch.dict(self.broker_app.ipam_caches, clear=True),
        ):
            cfg.parker.ipam_cache = config.Parker.IPAMCache(snapshot_dir="/var/lib")
            self.assertEqual(self.broker_app._load_parker_ipam(), cache.return_value)
            self.assertEqual(
                self.broker_app.ipam_caches, {"default": cache.return_value}
            )
            args, kwargs = cache.call_args
            self.assertEqual(args[0], "default")
            self.assertEqual(kwargs["snapshot_file"], "/var/lib/ipam-default.json")
            self.assertEqual(kwargs["reserve"], 32)
            self.assertEqual(args[1](), "netbox-ipam")
        netbox.assert_called_once_with(
            api_url="https://netbox", token="token", xlat=False, v6_filter=None
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
        mqtt.subscribe.return_value = (0, 1)
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

    def _site_ipams(self, stored=None, counts=None, offline=()):
        counts = counts or {}
        site_ipams = {}
        for site, net in (("muc", "2001:db8:10::/63"), ("vie", "2001:db8:20::/63")):
            site_ipams[site] = mock.MagicMock()
            site_ipams[site].get_or_allocate_prefix.return_value = (
                None,
                ipaddress.IPv6Network(net),
                [f"{site}1"] if site == stored else [],
            )
            site_ipams[site].is_primary.return_value = site == stored
            site_ipams[site].count_primary.return_value = counts.get(site, 0)
        for name in ("muc1", "muc2", "vie1"):
            self.broker_app.parker_worker_metrics.set_online(name)
            self.broker_app.parker_worker_data[name] = {
                "LinkAddress": "fe80::1",
                "ExternalAddress": f"{name}.example",
                "Port": 10000,
                "PublicKey": f"{name}-key",
            }
        for name in offline:
            self.broker_app.parker_worker_metrics.set_offline(name)
        return site_ipams

    def _multisite_exchange(self, query, cfg=None, best=(), **ipam_args):
        cfg = cfg or _multisite_config()
        site_ipams = self._site_ipams(**ipam_args)
        with (
            mock.patch.object(self.broker_app.config, "get_config", return_value=cfg),
            mock.patch.object(self.broker_app, "ipam", None),
            mock.patch.object(self.broker_app, "site_ipams", site_ipams),
            mock.patch.object(self.broker_app, "mqtt") as mqtt,
            mock.patch.object(
                self.broker_app.parker_worker_metrics,
                "get_best_workers",
                return_value=[
                    self.broker_app.WorkerResult(name, 0, 0, 0, 0) for name in best
                ],
            ),
            mock.patch.object(
                self.broker_app, "sign_response", return_value=b"signature"
            ),
        ):
            response = self.broker_app.app.test_client().get(
                "/config",
                query_string={"pubkey": _PUBLIC_KEY, "nonce": "n", **query},
            )
        self.assertEqual(response.status_code, 200, response.data)
        published = [
            (call.args[0], json.loads(call.args[1]))
            for call in mqtt.publish.call_args_list
        ]
        return json.loads(response.data.split(b"\n", 1)[0]), published, site_ipams

    def test_multisite_exchange_gives_all_sites(self):
        payload, published, site_ipams = self._multisite_exchange(
            {},
            best=("muc2", "vie1"),
            counts={"muc": 5, "vie": 3},
        )

        # A new node gets the site with the lowest load.
        self.assertEqual(payload["primary_site"], "vie")
        self.assertEqual(payload["range6"], "2001:db8:20::/64")
        self.assertEqual(payload["xlat_range6"], "2001:db8:20:1::/64")
        self.assertEqual([c["id"] for c in payload["concentrators"]], [101, 102])
        self.assertEqual(
            [c["endpoint"] for c in payload["concentrators"]],
            ["muc2.example:10000", "vie1.example:10000"],
        )
        muc = payload["sites"]["muc"]
        self.assertEqual(muc["range6"], "2001:db8:10::/64")
        self.assertEqual(muc["dns64"], "2001:db8:1::64")
        self.assertEqual(muc["concentrator"], 101)
        self.assertEqual(
            [(p["name"], p["id"], p["preferred"]) for p in muc["pgws"]],
            [("muc1", 1, False), ("muc2", 2, True)],
        )
        self.assertEqual(
            [(topic, data["Range6"]) for topic, data in published],
            [
                ("parker/wireguard/muc1", "2001:db8:10::/63"),
                ("parker/wireguard/muc2", "2001:db8:10::/63"),
                ("parker/wireguard/vie1", "2001:db8:20::/63"),
            ],
        )
        site_ipams["muc"].update_prefix.assert_called_once_with(
            _PUBLIC_KEY, False, True, ["muc2"], primary=False
        )
        site_ipams["vie"].update_prefix.assert_called_once_with(
            _PUBLIC_KEY, False, True, ["vie1"], primary=True
        )

    def test_multisite_primary_site_is_sticky(self):
        payload, _, site_ipams = self._multisite_exchange(
            {}, best=("muc1", "vie1"), stored="muc", counts={"muc": 9, "vie": 1}
        )

        self.assertEqual(payload["primary_site"], "muc")
        self.assertEqual(payload["range6"], "2001:db8:10::/64")
        site_ipams["muc"].update_prefix.assert_called_once_with(
            _PUBLIC_KEY, False, True, ["muc1"], primary=True
        )
        site_ipams["vie"].update_prefix.assert_called_once_with(
            _PUBLIC_KEY, False, True, ["vie1"], primary=False
        )

    def test_multisite_preferred_site_moves_node(self):
        payload, published, site_ipams = self._multisite_exchange(
            {"preferred_site": "vie"}, best=("muc1", "vie1"), stored="muc"
        )

        self.assertEqual(payload["primary_site"], "vie")
        self.assertEqual(payload["range6"], "2001:db8:20::/64")
        # The peer stays on all pgws of both sites.
        self.assertFalse(any(data.get("Remove") for _, data in published))
        site_ipams["muc"].update_prefix.assert_called_once_with(
            _PUBLIC_KEY, False, True, ["muc1"], primary=False
        )
        site_ipams["vie"].update_prefix.assert_called_once_with(
            _PUBLIC_KEY, False, True, ["vie1"], primary=True
        )

    def test_multisite_offline_primary_site_is_not_stored(self):
        payload, published, site_ipams = self._multisite_exchange(
            {},
            best=("vie1",),
            stored="muc",
            counts={"muc": 1, "vie": 9},
            offline=("muc1", "muc2"),
        )

        # The node gets vie for this response only. muc stays its primary site.
        self.assertEqual(payload["range6"], "2001:db8:20::/64")
        self.assertEqual(
            [(topic, data.get("Remove", False)) for topic, data in published],
            [("parker/wireguard/vie1", False)],
        )
        site_ipams["muc"].update_prefix.assert_not_called()
        site_ipams["vie"].update_prefix.assert_called_once_with(
            _PUBLIC_KEY, False, True, ["vie1"], primary=False
        )

        # When muc is online again, the node gets muc again.
        payload, _, _ = self._multisite_exchange(
            {}, best=("muc1",), stored="muc", counts={"muc": 1, "vie": 9}
        )
        self.assertEqual(payload["range6"], "2001:db8:10::/64")

    def test_select_primary_site_capacity_and_preference(self):
        select = self.broker_app._select_primary_site
        for vie_capacity, counts, stored, preferred, offline, expected in (
            # The preference is used when the site has capacity.
            (None, {"muc": 1, "vie": 9}, None, "vie", (), ("vie", "vie")),
            # A full, offline or unknown preferred site is ignored.
            (3, {"muc": 1, "vie": 3}, "muc", "vie", (), ("muc", "muc")),
            (None, {"muc": 1, "vie": 0}, "muc", "vie", ("vie1",), ("muc", "muc")),
            (None, {"muc": 1, "vie": 0}, "muc", "ber", (), ("muc", "muc")),
            # A site over capacity gives its nodes to other sites.
            (3, {"muc": 9, "vie": 4}, "vie", None, (), ("muc", "muc")),
            # A site with capacity 0 gets no new nodes, also when preferred.
            (0, {"muc": 9, "vie": 0}, None, "vie", (), ("muc", "muc")),
            # An offline stored site is kept, the response uses another site.
            (None, {"muc": 1, "vie": 9}, "muc", None, ("muc1", "muc2"), ("muc", "vie")),
            # A new node does not get an offline site.
            (None, {"muc": 1, "vie": 9}, None, None, ("muc1", "muc2"), ("vie", "vie")),
        ):
            with self.subTest(
                counts=counts, stored=stored, preferred=preferred, offline=offline
            ):
                cfg = _multisite_config(vie_capacity=vie_capacity)
                site_ipams = self._site_ipams(
                    stored=stored, counts=counts, offline=offline
                )
                with (
                    mock.patch.object(
                        self.broker_app.config, "get_config", return_value=cfg
                    ),
                    mock.patch.object(self.broker_app, "site_ipams", site_ipams),
                ):
                    self.assertEqual(
                        select(_PUBLIC_KEY, preferred), (stored, *expected)
                    )

    def test_select_primary_site_distributes_new_nodes_by_weight(self):
        for vie_weight, expected in (
            (1, {"muc": 6, "vie": 6}),
            (2, {"muc": 4, "vie": 8}),
        ):
            with self.subTest(vie_weight=vie_weight):
                cfg = _multisite_config(vie_weight=vie_weight)
                site_ipams = self._site_ipams()
                counts = {"muc": 0, "vie": 0}
                for name, site_ipam in site_ipams.items():
                    site_ipam.count_primary.side_effect = (
                        lambda name=name, counts=counts: counts[name]
                    )
                with (
                    mock.patch.object(
                        self.broker_app.config, "get_config", return_value=cfg
                    ),
                    mock.patch.object(self.broker_app, "site_ipams", site_ipams),
                ):
                    for _ in range(12):
                        _, site, _ = self.broker_app._select_primary_site(
                            _PUBLIC_KEY, None
                        )
                        counts[site] += 1
                    self.assertEqual(counts, expected)

    def test_ipam_messages_between_brokers(self):
        cache = mock.MagicMock()
        payload = {"site": "muc", "pubkey": _PUBLIC_KEY, "entry": {"prefix": "x"}}
        with (
            mock.patch.dict(self.broker_app.ipam_caches, {"muc": cache}, clear=True),
            mock.patch.object(self.broker_app, "mqtt") as mqtt,
        ):
            mqtt.connected = True
            self.broker_app._publish_ipam("muc", _PUBLIC_KEY, {"prefix": "x"})
            topic, data = mqtt.client.publish.call_args.args
            self.assertEqual(
                topic, f"wireguard-broker/{self.broker_app.broker_id}/ipam"
            )
            self.assertEqual(json.loads(data), payload)
            self.assertEqual(mqtt.client.publish.call_args.kwargs, {"qos": 1})
            mqtt.client.publish.return_value.wait_for_publish.assert_called_once()

            # Without EMQX, the broker does not wait.
            mqtt.connected = False
            mqtt.client.publish.reset_mock()
            self.broker_app._publish_ipam("muc", _PUBLIC_KEY, {"prefix": "x"})
            mqtt.client.publish.assert_not_called()

            handle = self.broker_app.handle_mqtt_message_ipam
            raw = json.dumps(payload).encode()
            handle(None, None, _message("wireguard-broker/other/ipam", raw))
            cache.apply.assert_called_once_with(_PUBLIC_KEY, {"prefix": "x"})
            # Own messages and invalid messages are ignored.
            own = f"wireguard-broker/{self.broker_app.broker_id}/ipam"
            handle(None, None, _message(own, raw))
            bad = json.dumps({**payload, "site": "ber"}).encode()
            handle(None, None, _message("wireguard-broker/other/ipam", bad))
            handle(None, None, _message("wireguard-broker/other/ipam", b"{"))
            cache.apply.assert_called_once()

    def test_switch_needs_fresh_handshake_on_new_pgw(self):
        cfg = _multisite_config()
        self.broker_app.handshakes.clear()
        query = {"pubkey": _PUBLIC_KEY, "site": "muc", "pgw": "muc2"}
        site_ipams = {"muc": mock.MagicMock(), "vie": mock.MagicMock()}
        with (
            mock.patch.object(self.broker_app.config, "get_config", return_value=cfg),
            mock.patch.object(self.broker_app, "mqtt") as mqtt,
            mock.patch.object(self.broker_app, "site_ipams", site_ipams),
        ):
            client = self.broker_app.app.test_client()
            self.assertEqual(
                client.get("/api/v3/switch", query_string=query).status_code, 409
            )
            bad = {**query, "pgw": "vie1"}
            self.assertEqual(
                client.get("/api/v3/switch", query_string=bad).status_code, 400
            )

            self.broker_app.handle_mqtt_message_parker_handshake(
                None,
                None,
                _message(
                    "parker/wireguard-worker/muc1/handshake",
                    json.dumps({"PublicKey": _PUBLIC_KEY}).encode(),
                ),
            )
            self.assertEqual(
                client.get("/api/v3/switch", query_string=query).status_code, 409
            )

            self.broker_app.handle_mqtt_message_parker_handshake(
                None,
                None,
                _message(
                    "parker/wireguard-worker/muc2/handshake",
                    json.dumps({"PublicKey": _PUBLIC_KEY}).encode(),
                ),
            )
            self.assertEqual(
                client.get("/api/v3/switch", query_string=query).status_code, 200
            )
            mqtt.publish.assert_called_once_with(
                "parker/wireguard/muc1",
                json.dumps({"PublicKey": _PUBLIC_KEY, "Unroute": True}).encode(),
                qos=1,
            )
            # The reported pgw is the new preferred pgw of the site.
            site_ipams["muc"].update_prefix.assert_called_once_with(
                _PUBLIC_KEY, False, True, ["muc2"]
            )
            site_ipams["vie"].update_prefix.assert_not_called()

            # When the broker cannot store the pgw, it does not unroute.
            mqtt.publish.reset_mock()
            site_ipams["muc"].update_prefix.side_effect = RuntimeError("down")
            self.assertEqual(
                client.get("/api/v3/switch", query_string=query).status_code, 500
            )
            mqtt.publish.assert_not_called()

        with (
            mock.patch.object(self.broker_app.config, "get_config", return_value=cfg),
            mock.patch.object(self.broker_app.time, "monotonic", return_value=10**9),
        ):
            response = self.broker_app.app.test_client().get(
                "/api/v3/switch", query_string=query
            )
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
