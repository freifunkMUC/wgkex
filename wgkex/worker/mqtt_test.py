"""Unit tests for mqtt.py"""
import socket
import threading
import unittest
from time import sleep

import mock
import paho.mqtt.client

from wgkex.common.mqtt import TOPIC_CONNECTED_PEERS
from wgkex.worker import mqtt


def _get_config_mock(domains=None, mqtt=None):
    test_prefix = "_ffmuc_"
    config_mock = mock.MagicMock()
    config_mock.domains = (
        domains if domains is not None else [f"{test_prefix}domain.one"]
    )
    config_mock.domain_prefix = test_prefix
    if mqtt:
        config_mock.mqtt = mqtt
    return config_mock


class MQTTTest(unittest.TestCase):
    @mock.patch.object(mqtt.mqtt, "Client")
    @mock.patch.object(mqtt.socket, "gethostname")
    @mock.patch.object(mqtt, "get_config")
    def test_connect_success(self, config_mock, hostname_mock, mqtt_mock):
        """Tests successful connection to MQTT server."""
        hostname_mock.return_value = "hostname"
        config_mqtt_mock = mock.MagicMock()
        config_mqtt_mock.broker_url = "some_url"
        config_mqtt_mock.broker_port = 1833
        config_mqtt_mock.keepalive = False
        config_mock.return_value = _get_config_mock(mqtt=config_mqtt_mock)
        ee = threading.Event()
        mqtt.connect(ee)
        ee.set()
        mqtt_mock.assert_has_calls(
            [mock.call().connect("some_url", port=1833, keepalive=False)],
            any_order=True,
        )

    @mock.patch.object(mqtt.mqtt, "Client")
    @mock.patch.object(mqtt, "get_config")
    def test_connect_fails_mqtt_error(self, config_mock, mqtt_mock):
        """Tests failure for connect - ValueError."""
        mqtt_mock.side_effect = ValueError("barf")
        config_mqtt_mock = mock.MagicMock()
        config_mqtt_mock.broker_url = "some_url"
        config_mock.return_value = _get_config_mock(mqtt=config_mqtt_mock)
        with self.assertRaises(ValueError):
            mqtt.connect(threading.Event())


    @mock.patch.object(mqtt, "get_config")
    @mock.patch.object(mqtt, "get_connected_peers_count")
    def test_publish_metrics_loop_success(self, conn_peers_mock, config_mock):
        config_mock.return_value = _get_config_mock()
        conn_peers_mock.return_value = 20
        mqtt_client = mock.MagicMock(spec=paho.mqtt.client.Client)

        ee = threading.Event()
        thread = threading.Thread(
            target=mqtt.publish_metrics_loop,
            args=(ee, mqtt_client, "_ffmuc_domain.one"),
        )
        thread.start()

        i = 0
        while i < 20 and not mqtt_client.publish.called:
            i += 1
            sleep(0.1)

        conn_peers_mock.assert_called_with("wg-domain.one")
        mqtt_client.publish.assert_called_with(
            TOPIC_CONNECTED_PEERS.format(
                domain="_ffmuc_domain.one", worker=socket.gethostname()
            ),
            20,
            retain=True,
        )

        ee.set()

        i = 0
        while i < 20 and thread.is_alive():
            i += 1
            sleep(0.1)

        self.assertFalse(thread.is_alive())


""" @mock.patch.object(msg_queue, "link_handler")
    @mock.patch.object(mqtt, "get_config")
    def test_on_message_wireguard_success(self, config_mock, link_mock):
        # Tests on_message for success.
        config_mock.return_value = _get_config_mock()
        link_mock.return_value = dict(WireGuard="result")
        mqtt_msg = mock.patch.object(mqtt.mqtt, "MQTTMessage")
        mqtt_msg.topic = "wireguard/_ffmuc_domain1/gateway"
        mqtt_msg.payload = b"PUB_KEY"
        mqtt.on_message_wireguard(None, None, mqtt_msg)
        link_mock.assert_has_calls(
            [
                mock.call(
                    msg_queue.WireGuardClient(
                        public_key="PUB_KEY", domain="domain1", remove=False
                    )
                )
            ],
            any_order=True,
        )

    @mock.patch.object(msg_queue, "link_handler")
    @mock.patch.object(mqtt, "get_config")
    def test_on_message_wireguard_fails_no_domain(self, config_mock, link_mock):
        # Tests on_message for failure to parse domain.
        config_mqtt_mock = mock.MagicMock()
        config_mqtt_mock.broker_url = "mqtt://broker"
        config_mqtt_mock.broker_port = 1883
        config_mqtt_mock.keepalive = 5
        config_mqtt_mock.password = "pass"
        config_mqtt_mock.tls = True
        config_mqtt_mock.username = "user"
        config_mock.return_value = _get_config_mock(
            domains=["a", "b"], mqtt=config_mqtt_mock
        )
        link_mock.return_value = dict(WireGuard="result")
        mqtt_msg = mock.patch.object(mqtt.mqtt, "MQTTMessage")
        mqtt_msg.topic = "wireguard/bad_domain_match"
        with self.assertRaises(ValueError):
            mqtt.on_message_wireguard(None, None, mqtt_msg)
"""


if __name__ == "__main__":
    unittest.main()
