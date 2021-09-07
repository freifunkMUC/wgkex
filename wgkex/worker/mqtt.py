#!/usr/bin/env python3
"""Process messages from MQTT."""

import paho.mqtt.client as mqtt

# TODO(ruairi): Deprecate __init__.py from config, as it masks namespace.
from wgkex.config.config import load_config
import socket
import re
from wgkex.worker.netlink import link_handler
from wgkex.worker.netlink import WireGuardClient
from typing import Optional, Dict, List, Any, Union


def fetch_from_config(var: str) -> Optional[Union[Dict[str, str], str]]:
    """Fetches values from configuration file.

    Arguments:
        var: The variable to fetch from config.

    Returns:
        The given variable from configuration.
    """
    config = load_config()
    return config.get(var)


def connect(domains: List[str]) -> None:
    """Connect to MQTT for the given domains.

    Argument:
        domains: The domains to connect to.
    """
    broker_address = fetch_from_config("mqtt").get("broker_url")
    broker_port = fetch_from_config("mqtt").get("broker_port")
    broker_keepalive = fetch_from_config("mqtt").get("keepalive")
    # TODO(ruairi): Move the hostname to a global variable.
    client = mqtt.Client(socket.gethostname())
    client.on_message = on_message
    print(f"connecting to broker {broker_address}")
    client.connect(broker_address, port=broker_port, keepalive=broker_keepalive)
    for domain in domains:
        topic = f"wireguard/{domain}/+"
        print(f"Subscribing to topic {topic}")
        client.subscribe(topic)
    client.loop_forever()


def on_message(client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
    """Processes messages from MQTT and forwards them to netlink.

    Arguments:
        client: the client instance for this callback.
        userdata: the private user data.
        message: The MQTT message.
    """
    # TODO(ruairi): Check bounds and raise exception here.
    domain = re.search(r"/.*ffmuc_(\w+)/", message.topic).group(1)
    client = WireGuardClient(
        public_key=str(message.payload.decode("utf-8")),
        domain=domain,
        remove=False,
    )
    print(f"Received node create message for key {client.public_key}")
    # TODO(ruairi): Verify return type here.
    print(link_handler(client))
