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
        self.assertEqual(broker_app.join_host_port("::1", "51820"), "[::1]:51820")

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
