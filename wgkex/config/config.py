import os
import sys

import yaml
from voluptuous import All, MultipleInvalid, Required, Schema

MQTT_SCHEMA = Schema(
    {
        Required("broker_url"): str,
        Required("broker_port", default=1883): int,
        Required("username", default=""): str,
        Required("password", default=""): str,
        Required("keepalive", default=5): int,
        Required("tls", default=False): bool,
    }
)

CONFIG_SCHEMA = Schema(
    {
        Required("domains"): All([str], min=1),
        Required("pubkeys_file", default=""): str,
        Required("mqtt"): MQTT_SCHEMA,
    }
)


def load_config():
    config_file = os.environ.get("WGKEX_CONFIG_FILE", "/etc/wgkex.yaml")
    with open(config_file, "r") as stream:
        try:
            config = CONFIG_SCHEMA(yaml.safe_load(stream))
        except MultipleInvalid as ex:
            print(f"Config file failed to validate: {ex}", file=sys.stderr)
            sys.exit(1)
    return config
