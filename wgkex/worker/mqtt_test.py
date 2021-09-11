"""Unit tests for mqtt.py"""
import unittest
import mock
import mqtt


class MQTTTest(unittest.TestCase):
    @mock.patch.object(mqtt, "load_config")
    def test_fetch_from_config_success(self, config_mock):
        """Ensure we can fetch a value from config."""
        config_mock.return_value = dict(key="value")
        self.assertEqual("value", mqtt.fetch_from_config("key"))

    @mock.patch.object(mqtt, "load_config")
    def test_fetch_from_config_fails_no_key(self, config_mock):
        """Tests we fail with ValueError for missing key in config."""
        config_mock.return_value = dict(key="value")
        with self.assertRaises(ValueError):
            mqtt.fetch_from_config("does_not_exist")

    @mock.patch.object(mqtt.mqtt, "Client")
    @mock.patch.object(mqtt.socket, "gethostname")
    @mock.patch.object(mqtt, "load_config")
    def test_connect_success(self, config_mock, hostname_mock, mqtt_mock):
        """Tests successful connection to MQTT server."""
        hostname_mock.return_value = "hostname"
        config_mock.return_value = dict(mqtt={"broker_url": "some_url"})
        mqtt.connect()
        mqtt_mock.assert_has_calls(
            [mock.call().connect("some_url", port=None, keepalive=None)],
            any_order=True,
        )

    @mock.patch.object(mqtt.mqtt, "Client")
    @mock.patch.object(mqtt, "load_config")
    def test_connect_fails_mqtt_error(self, config_mock, mqtt_mock):
        """Tests failure for connect - ValueError."""
        mqtt_mock.side_effect = ValueError("barf")
        config_mock.return_value = dict(mqtt={"broker_url": "some_url"})
        with self.assertRaises(ValueError):
            mqtt.connect()

    @mock.patch.object(mqtt, "link_handler")
    @mock.patch.object(mqtt, "load_config")
    def test_on_message_success(self, config_mock, link_mock):
        """Tests on_message for success."""
        config_mock.return_value = {"domain_prefix": "_ffmuc_"}
        link_mock.return_value = dict(WireGuard="result")
        mqtt_msg = mock.patch.object(mqtt.mqtt, "MQTTMessage")
        mqtt_msg.topic = "/_ffmuc_domain1/"
        mqtt_msg.payload = b"PUB_KEY"
        mqtt.on_message(None, None, mqtt_msg)
        link_mock.assert_has_calls(
            [
                mock.call(
                    mqtt.WireGuardClient(
                        public_key="PUB_KEY", domain="domain1", remove=False
                    )
                )
            ],
            any_order=True,
        )

    @mock.patch.object(mqtt, "link_handler")
    @mock.patch.object(mqtt, "load_config")
    def test_on_message_fails_no_domain(self, config_mock, link_mock):
        """Tests on_message for failure to parse domain."""
        config_mock.return_value = {
            "domain_prefix": "ffmuc_",
            "log_level": "DEBUG",
            "domains": ["a", "b"],
            "mqtt": {
                "broker_port": 1883,
                "broker_url": "mqtt://broker",
                "keepalive": 5,
                "password": "pass",
                "tls": True,
                "username": "user",
            },
        }
        link_mock.return_value = dict(WireGuard="result")
        mqtt_msg = mock.patch.object(mqtt.mqtt, "MQTTMessage")
        mqtt_msg.topic = "bad_domain_match"
        with self.assertRaises(ValueError):
            mqtt.on_message(None, None, mqtt_msg)


if __name__ == "__main__":
    unittest.main()
