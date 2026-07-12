import importlib
import sys
import unittest

import flask_mqtt
import mock

from wgkex.config import config


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


class TestBrokerStartup(unittest.TestCase):
    def tearDown(self) -> None:
        config._parsed_config = None
        sys.modules.pop("wgkex.broker.app", None)
        sys.modules.pop("wgkex.broker.signer", None)

    def test_broker_imports_with_default_parker_disabled_config(self):
        config._parsed_config = config.Config.from_dict(
            {
                "domains": [],
                "domain_prefixes": [],
                "mqtt": {"broker_url": "", "username": "", "password": ""},
            }
        )

        with mock.patch.object(flask_mqtt, "Mqtt", MqttStub):
            broker_app = importlib.import_module("wgkex.broker.app")

        self.assertFalse(config.get_config().parker.enabled)
        self.assertIsNone(broker_app.ipam)


if __name__ == "__main__":
    unittest.main()
