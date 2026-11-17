import importlib
import sys
import unittest
from unittest import mock

import flask_mqtt

import wgkex.broker
from wgkex.config import config


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


class TestBrokerStartup(unittest.TestCase):
    def tearDown(self) -> None:
        config._parsed_config = None
        _evict_broker_modules("app", "signer")

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

    def test_parker_startup_eagerly_rejects_invalid_signing_key(self):
        config._parsed_config = config.Config.from_dict(
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
                "broker_signing_key": "YQ==",
                "domains": [],
                "domain_prefixes": [],
                "mqtt": {"broker_url": "", "username": "", "password": ""},
            }
        )

        with self.assertRaisesRegex(ValueError, "Invalid private key length"):
            importlib.import_module("wgkex.broker.signer")


if __name__ == "__main__":
    unittest.main()
