import ipaddress
import json
import mock
import sys
import unittest
import paho.mqtt.client

from wgkex.config import config


def _get_test_config() -> config.Config:
    test_config = config.Config.from_dict(
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
            "broker_signing_key": "longstring",
            "domains": [],
            "domain_prefixes": "",
            "workers": {},
            "mqtt": {"broker_url": "", "username": "", "password": ""},
        }
    )
    return test_config


class TestParkerQuery(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Give each test a placeholder config
        test_config = _get_test_config()
        config._parsed_config = test_config

    @classmethod
    def tearDownClass(cls) -> None:
        config._parsed_config = None

    def test_parker_query_from_dict_valid(self):
        from wgkex.broker.parker import ParkerQuery

        data = {
            "v6mtu": "1400",
            "pubkey": "TszFS3oFRdhsJP3K0VOlklGMGYZy+oFCtlaghXJqW2g=",
            "nonce": "n1",
        }
        q = ParkerQuery.from_dict(data)
        self.assertEqual(q.v6mtu, 1400)
        self.assertEqual(q.pubkey, data["pubkey"])
        self.assertEqual(q.nonce, "n1")

    def test_parker_query_from_dict_invalid_pubkey(self):
        from wgkex.broker.parker import ParkerQuery

        data = {
            "v6mtu": "1400",
            "pubkey": "invalidkey",
            "nonce": "n1",
        }
        with self.assertRaises(ValueError):
            ParkerQuery.from_dict(data)


class TestParker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Give each test a placeholder config
        test_config = _get_test_config()
        config._parsed_config = test_config

        # Stub out signer module before importing broker.app which imports it at module level
        sys.modules["wgkex.broker.signer"] = mock.MagicMock()

        # Stub out flask_mqtt.Mqtt to avoid network connections during import
        flask_mqtt_stub = mock.MagicMock()

        class MqttStub:
            def __init__(self, *args, **kwargs):
                pass

            def init_app(self, app, **kwargs):
                return None

            def on_topic(self, topic=None):
                def _decorator(func):
                    return func

                return _decorator

            def on_connect(self):
                def _decorator(func):
                    return func

                return _decorator

            def on_message(self):
                def _decorator(func):
                    return func

                return _decorator

            def message_callback_add(self, *args, **kwargs):
                return None

            def subscribe(self, *args, **kwargs):
                return None

            def publish(self, *args, **kwargs):
                return None

        flask_mqtt_stub.Mqtt = MqttStub
        sys.modules["flask_mqtt"] = flask_mqtt_stub

    @classmethod
    def tearDownClass(cls) -> None:
        config._parsed_config = None

    def test_join_host_port(self):
        from wgkex.broker import app as broker_app

        # Test join_host_port
        self.assertEqual(broker_app.join_host_port("1.2.3.4", "51820"), "1.2.3.4:51820")
        self.assertEqual(
            broker_app.join_host_port("worker.example", "51820"),
            "worker.example:51820",
        )
        self.assertEqual(broker_app.join_host_port("::1", "51820"), "[::1]:51820")

    def test_config_route_formats_ipv6_endpoint(self):
        from wgkex.broker import app as broker_app
        from wgkex.broker.metrics import WorkerResult

        ipam = mock.MagicMock()
        ipam.get_or_allocate_prefix.return_value = (
            None,
            ipaddress.IPv6Network("2001:db8:42::/63"),
            ["worker1"],
        )
        worker = WorkerResult(name="worker1", id=1, diff=0, peers=0, target=0)
        broker_app.parker_worker_data["worker1"] = {
            "LinkAddress": "fe80::1",
            "ExternalAddress": "2001:db8::1",
            "Port": 51820,
            "PublicKey": "worker-key",
        }

        query = {
            "pubkey": "TszFS3oFRdhsJP3K0VOlklGMGYZy+oFCtlaghXJqW2g=",
            "nonce": "nonce",
        }
        for route in ("/config", "/api/v3/config", "/api/v3/wg/key/exchange"):
            with (
                self.subTest(route=route),
                mock.patch.object(broker_app, "ipam", ipam),
                mock.patch.object(
                    broker_app.parker_worker_metrics,
                    "get_best_workers",
                    return_value=[worker],
                ),
                mock.patch.object(
                    broker_app, "sign_response", return_value=b"signature"
                ),
            ):
                response = broker_app.app.test_client().get(route, query_string=query)

            self.assertEqual(response.status_code, 200)
            payload = json.loads(response.data.split(b"\n", 1)[0])
            self.assertEqual(
                payload["concentrators"][0]["endpoint"], "[2001:db8::1]:51820"
            )
        routes = {rule.rule for rule in broker_app.app.url_map.iter_rules()}
        self.assertIn("/api/v3/config", routes)
        self.assertIn(
            "/api/v3/wg/key/exchange",
            routes,
        )

    def test_config_route_rejects_prefix_without_two_64s(self):
        from wgkex.broker import app as broker_app

        ipam = mock.MagicMock()
        ipam.get_or_allocate_prefix.return_value = (
            None,
            ipaddress.IPv6Network("2001:db8:42::/64"),
            [],
        )
        query = {
            "pubkey": "TszFS3oFRdhsJP3K0VOlklGMGYZy+oFCtlaghXJqW2g=",
            "nonce": "nonce",
        }
        with mock.patch.object(broker_app, "ipam", ipam):
            response = broker_app.app.test_client().get("/config", query_string=query)

        self.assertEqual(response.status_code, 500)

    def test_parker_mqtt_connect_keeps_legacy_discovery(self):
        from wgkex.broker import app as broker_app
        from wgkex.common.mqtt import MQTTTopics

        mqtt = mock.MagicMock()
        client = mock.MagicMock(host="mqtt", port=1883)
        with mock.patch.object(broker_app, "mqtt", mqtt):
            broker_app.handle_mqtt_connect(
                client,
                b"",
                {},
                paho.mqtt.enums.ConnackCode.CONNACK_ACCEPTED,
            )

        expected_subscriptions = {
            MQTTTopics.TOPIC_CONNECTED_PEERS.format(worker="+", domain="+"),
            MQTTTopics.TOPIC_WORKER_STATUS.format(worker="+"),
            MQTTTopics.TOPIC_WORKER_WG_DATA.format(worker="+", domain="+"),
            MQTTTopics.TOPIC_BROKER_ANNOUNCE.format(broker="+"),
            MQTTTopics.TOPIC_PARKER_CONNECTED_PEERS.format(worker="+"),
            MQTTTopics.TOPIC_PARKER_WORKER_STATUS.format(worker="+"),
            MQTTTopics.TOPIC_PARKER_WORKER_WG_DATA.format(worker="+"),
            MQTTTopics.TOPIC_PARKER_BROKER_ANNOUNCE.format(broker="+"),
        }
        self.assertEqual(
            {call.args[0] for call in mqtt.subscribe.call_args_list},
            expected_subscriptions,
        )
        legacy_topic = MQTTTopics.TOPIC_BROKER_ANNOUNCE.format(
            broker=broker_app._HOSTNAME
        )
        parker_topic = MQTTTopics.TOPIC_PARKER_BROKER_ANNOUNCE.format(
            broker=broker_app._HOSTNAME
        )
        mqtt.publish.assert_has_calls(
            [
                mock.call(legacy_topic, 1, qos=1, retain=True),
                mock.call(parker_topic, b"", qos=1, retain=True),
                mock.call(parker_topic, 1, qos=1, retain=False),
            ]
        )
        self.assertEqual(broker_app.app.config["MQTT_LAST_WILL_TOPIC"], legacy_topic)

    def test_parker_mqtt_handlers(self):
        # Import app after config is set
        from wgkex.broker import app as broker_app

        # Test parker mqtt data handler
        broker_app.parker_worker_data.clear()

        payload = {
            "LinkAddress": "fe80::1",
            "ExternalAddress": "host",
            "Port": 51820,
            "PublicKey": "K==",
        }
        msg = paho.mqtt.client.MQTTMessage()
        msg.topic = "parker/wireguard-worker/worker1/data".encode("utf-8")
        msg.payload = json.dumps(payload).encode("utf-8")
        broker_app.handle_mqtt_message_parker_data(None, None, msg)
        self.assertIn("worker1", broker_app.parker_worker_data)
        self.assertEqual(broker_app.parker_worker_data["worker1"], payload)

        # Test parker broker announce handler
        broker_app.parker_active_brokers.clear()
        msg.topic = "parker/wireguard-broker/broker1/status".encode("utf-8")
        msg.payload = b"1"
        broker_app.handle_mqtt_message_parker_broker_status(None, None, msg)
        self.assertIn("broker1", broker_app.parker_active_brokers)

        msg.payload = b"0"
        broker_app.handle_mqtt_message_parker_broker_status(None, None, msg)
        self.assertNotIn("broker1", broker_app.parker_active_brokers)

        broker_app.active_brokers.clear()
        broker_app.parker_active_brokers.clear()
        msg.topic = "wireguard-broker/broker1/status".encode("utf-8")
        msg.payload = b"1"
        broker_app.handle_mqtt_message_broker_status(None, None, msg)
        self.assertIn("broker1", broker_app.active_brokers)
        self.assertIn("broker1", broker_app.parker_active_brokers)
        msg.payload = b"0"
        broker_app.handle_mqtt_message_broker_status(None, None, msg)
        self.assertNotIn("broker1", broker_app.active_brokers)
        self.assertNotIn("broker1", broker_app.parker_active_brokers)

    def test_get_active_brokers_count(self):
        from wgkex.broker import app as broker_app

        broker_app.active_brokers.clear()
        broker_app.parker_active_brokers.clear()
        # Test that count never goes below 1
        self.assertEqual(broker_app.get_active_brokers_count(parker=False), 1)
        self.assertEqual(broker_app.get_active_brokers_count(parker=True), 1)

        broker_app.active_brokers.add("a")
        self.assertEqual(broker_app.get_active_brokers_count(parker=False), 1)
        broker_app.active_brokers.add("b")
        self.assertEqual(broker_app.get_active_brokers_count(parker=False), 2)

    def test_ipam_loaded(self):
        from wgkex.broker import app as broker_app
        from wgkex.broker.ipam_json import JSONFileIPAM

        # ipam should be loaded and be a JSONFileIPAM instance
        self.assertIsNotNone(broker_app.ipam)
        self.assertIsInstance(broker_app.ipam, JSONFileIPAM)

    def test_ipam_and_parker_metrics(self):
        from wgkex.broker import app as broker_app

        # Test parker metrics handler updates parker_worker_metrics
        class Msg:
            pass

        msg = paho.mqtt.client.MQTTMessage()
        msg.topic = "parker/wireguard-metrics/workerX/connected_peers".encode("utf-8")
        msg.payload = b"5"
        broker_app.handle_mqtt_message_parker_metrics(None, None, msg)
        self.assertEqual(
            broker_app.parker_worker_metrics.get("workerX")
            .get_domain_metrics("parker")
            .get("connected_peers"),
            5,
        )
        self.assertEqual(broker_app.parker_worker_metrics.get_total_peer_count(), 5)


if __name__ == "__main__":
    unittest.main()
