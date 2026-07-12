import importlib
import ipaddress
import json
import sys
import unittest

import flask_mqtt
import mock
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
        self.broker_app.active_brokers.clear()
        self.broker_app.parker_active_brokers.clear()
        self.broker_app.worker_metrics.data.clear()
        self.broker_app.parker_worker_metrics.data.clear()

    def test_index_and_v1_exchange_routes(self):
        with mock.patch.object(
            self.broker_app, "render_template", return_value="index"
        ):
            response = self.broker_app.app.test_client().get("/")
        self.assertEqual(response.text, "index")

        with mock.patch.object(self.broker_app, "mqtt") as mqtt:
            response = self.broker_app.app.test_client().post(
                "/api/v1/wg/key/exchange",
                json={"public_key": _PUBLIC_KEY, "domain": _DOMAIN},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"Message": "OK"})
        mqtt.publish.assert_called_once_with(f"wireguard/{_DOMAIN}/all", _PUBLIC_KEY)

        response = self.broker_app.app.test_client().post(
            "/api/v1/wg/key/exchange",
            json={"public_key": "invalid", "domain": "unknown"},
        )
        self.assertEqual(response.status_code, 400)

    def test_v2_exchange_error_and_success_paths(self):
        response = self.broker_app.app.test_client().post(
            "/api/v2/wg/key/exchange",
            json={"public_key": "invalid", "domain": _DOMAIN},
        )
        self.assertEqual(response.status_code, 400)

        with mock.patch.object(
            self.broker_app.worker_metrics,
            "get_best_worker",
            return_value=(None, 0, 0),
        ):
            response = self.broker_app.app.test_client().post(
                "/api/v2/wg/key/exchange",
                json={"public_key": _PUBLIC_KEY, "domain": _DOMAIN},
            )
        self.assertEqual(response.status_code, 400)

        with mock.patch.object(
            self.broker_app.worker_metrics,
            "get_best_worker",
            return_value=("worker", 0, 1),
        ):
            response = self.broker_app.app.test_client().post(
                "/api/v2/wg/key/exchange",
                json={"public_key": _PUBLIC_KEY, "domain": _DOMAIN},
            )
        self.assertEqual(response.status_code, 500)

        self.broker_app.worker_data[("worker", _DOMAIN)] = {
            "ExternalAddress": "gateway.example",
            "Port": 51820,
            "LinkAddress": "fe80::1",
            "PublicKey": "gateway-key",
        }
        metrics = mock.MagicMock()
        metrics.get_domain_metrics.return_value = {"connected_peers": 3}
        self.broker_app.active_brokers.update({"broker-a", "broker-b"})
        with (
            mock.patch.object(
                self.broker_app.worker_metrics,
                "get_best_worker",
                return_value=("worker", 0, 3),
            ),
            mock.patch.object(
                self.broker_app.worker_metrics, "get", return_value=metrics
            ),
            mock.patch.object(self.broker_app.worker_metrics, "update") as update,
        ):
            response = self.broker_app.app.test_client().post(
                "/api/v2/wg/key/exchange",
                json={"public_key": _PUBLIC_KEY, "domain": _DOMAIN},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json["Endpoint"],
            {
                "Address": "gateway.example",
                "Port": "51820",
                "AllowedIPs": ["fe80::1"],
                "PublicKey": "gateway-key",
            },
        )
        update.assert_called_once_with("worker", _DOMAIN, "connected_peers", 5)

    def test_load_parker_ipam_modes(self):
        self.assertIsNone(self.broker_app._load_parker_ipam())

        cfg = mock.MagicMock()
        cfg.parker.enabled = True
        cfg.parker.xlat = True
        cfg.parker.ipam = config.Parker.IPAM.JSON
        with (
            mock.patch.object(self.broker_app.config, "get_config", return_value=cfg),
            mock.patch.object(
                self.broker_app, "JSONFileIPAM", return_value="json-ipam"
            ),
        ):
            self.assertEqual(self.broker_app._load_parker_ipam(), "json-ipam")

        cfg.parker.ipam = config.Parker.IPAM.NETBOX
        cfg.netbox = config.Netbox(url="https://netbox", api_key="token")
        with (
            mock.patch.object(self.broker_app.config, "get_config", return_value=cfg),
            mock.patch.object(
                self.broker_app, "NetboxIPAM", return_value="netbox-ipam"
            ) as netbox,
        ):
            self.assertEqual(self.broker_app._load_parker_ipam(), "netbox-ipam")
        netbox.assert_called_once_with(
            api_url="https://netbox", token="token", xlat=True
        )

        cfg.netbox = None
        with (
            mock.patch.object(self.broker_app.config, "get_config", return_value=cfg),
            self.assertRaisesRegex(Exception, "Missing config for NetBox"),
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
        with mock.patch.object(self.broker_app, "mqtt", mqtt):
            self.broker_app.handle_mqtt_connect(
                client, b"", {}, paho.mqtt.enums.ConnackCode.CONNACK_ACCEPTED
            )
        self.assertEqual(mqtt.subscribe.call_count, 4)
        mqtt.publish.assert_called_once_with(mock.ANY, 1, qos=1, retain=True)

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

    def test_broker_status_and_fallback_handlers(self):
        message = _message("wireguard-broker/broker/status", b"invalid")
        self.broker_app.handle_mqtt_message_broker_status(None, b"", message)
        self.assertNotIn("broker", self.broker_app.active_brokers)

        message.payload = b"1"
        self.broker_app.handle_mqtt_message_broker_status(None, b"", message)
        self.assertIn("broker", self.broker_app.active_brokers)
        message.payload = b"0"
        self.broker_app.handle_mqtt_message_broker_status(None, b"", message)
        self.assertNotIn("broker", self.broker_app.active_brokers)

        self.broker_app.handle_mqtt_message(
            None, b"", _message("unhandled/topic", b"payload")
        )

    def test_non_parker_shutdown_uses_legacy_announcement(self):
        with (
            mock.patch.object(self.broker_app, "mqtt") as mqtt,
            mock.patch.object(self.broker_app.time, "sleep") as sleep,
            mock.patch.object(self.broker_app.sys, "exit") as exit_process,
        ):
            self.broker_app._publish_offline_and_exit(None, None)

        mqtt.publish.assert_called_once_with(mock.ANY, 0, qos=1, retain=True)
        sleep.assert_called_once_with(2)
        exit_process.assert_called_once_with(0)

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

            self.broker_app.parker_worker_data["worker"] = {
                "LinkAddress": "fe80::1",
                "ExternalAddress": "gateway.example",
                "Port": 51820,
                "PublicKey": "gateway-key",
            }
            # An unconfigured worker must not be used as emergency fallback:
            # its concentrator ID would not be unique or stable.
            response = self.broker_app.app.test_client().get(
                "/config", query_string=query
            )
            self.assertEqual(response.status_code, 400)

            # A configured worker is used as fallback with its configured ID.
            cfg.workers = config.Workers.from_dict({"worker": {"id": 7}}, 10)
            with mock.patch.object(
                self.broker_app, "sign_response", return_value=b"signature"
            ):
                response = self.broker_app.app.test_client().get(
                    "/config", query_string=query
                )
            self.assertEqual(response.status_code, 200)
            payload = json.loads(response.data.split(b"\n", 1)[0])
            self.assertEqual(payload["concentrators"][0]["id"], 7)

            self.broker_app.parker_worker_data.clear()
            with mock.patch.object(
                self.broker_app.parker_worker_metrics,
                "get_best_workers",
                return_value=[worker],
            ):
                response = self.broker_app.app.test_client().get(
                    "/config", query_string=query
                )
            self.assertEqual(response.status_code, 500)

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

        invalid_status = _message("parker/wireguard-broker/broker/status", b"invalid")
        self.broker_app.handle_mqtt_message_parker_broker_status(
            None, b"", invalid_status
        )
        self.assertNotIn("broker", self.broker_app.parker_active_brokers)

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

    def test_shutdown_still_exits_when_publish_fails(self):
        with (
            mock.patch.object(
                self.broker_app.mqtt,
                "publish",
                side_effect=RuntimeError("publish failed"),
            ),
            mock.patch.object(self.broker_app.sys, "exit") as exit_process,
        ):
            self.broker_app._publish_offline_and_exit(None, None)
        exit_process.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
