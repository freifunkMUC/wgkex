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
        self.assertIsNone(mqtt.fetch_from_config("does_not_exist"))

    @mock.patch.object(mqtt.mqtt, "Client")
    @mock.patch.object(mqtt.socket, "gethostname")
    @mock.patch.object(mqtt, "load_config")
    def test_connect_success(self, config_mock, hostname_mock, mqtt_mock):
        """Tests successful connection to MQTT server."""
        hostname_mock.return_value = "hostname"
        config_mock.return_value = dict(mqtt={"broker_url": "some_url"})
        mqtt.connect(["domain1", "domain2"])
        mqtt_mock.assert_has_calls(
            [
                mock.call("hostname"),
                mock.call().connect("some_url"),
                mock.call().subscribe("wireguard/domain1/+"),
                mock.call().subscribe("wireguard/domain2/+"),
                mock.call().loop_forever(),
            ],
            any_order=True,
        )

    @mock.patch.object(mqtt.mqtt, "Client")
    @mock.patch.object(mqtt, "load_config")
    def test_connect_fails_mqtt_error(self, config_mock, mqtt_mock):
        """Tests failure for connect - ValueError."""
        mqtt_mock.side_effect = ValueError("barf")
        config_mock.return_value = dict(mqtt={"broker_url": "some_url"})
        with self.assertRaises(ValueError):
            mqtt.connect(["domain1", "domain2"])

    @mock.patch.object(mqtt, "link_handler")
    def test_on_message_success(self, link_mock):
        """Tests on_message for success."""
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
    def test_on_message_fails_no_domain(self, link_mock):
        """Tests on_message for failure to parse domain."""
        link_mock.return_value = dict(WireGuard="result")
        mqtt_msg = mock.patch.object(mqtt.mqtt, "MQTTMessage")
        mqtt_msg.topic = "bad_domain_match"
        with self.assertRaises(AttributeError):
            mqtt.on_message(None, None, mqtt_msg)


if __name__ == "__main__":
    unittest.main()
