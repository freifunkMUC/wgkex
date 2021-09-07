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
import logging


def fetch_from_config(var: str) -> Optional[Union[Dict[str, str], str]]:
    """Fetches values from configuration file.

    Arguments:
        var: The variable to fetch from config.

    Raises:
        ValueError: If given key cannot be found in configuration.

    Returns:
        The given variable from configuration.
    """
    config = load_config()
    ret = config.get(var)
    if not ret:
        raise ValueError("Failed to get %s from configuration, failing", var)
    return config.get(var)


def connect(domains: List[str]) -> None:
    """Connect to MQTT for the given domains.

    Argument:
        domains: The domains to connect to.
    """
    if not domains:
        logging.error("No domains were passed: %s", domains)
    base_config = fetch_from_config("mqtt")
    broker_address = base_config.get("broker_url")
    broker_port = base_config.get("broker_port")
    broker_keepalive = base_config.get("keepalive")
    # TODO(ruairi): Move the hostname to a global variable.
    client = mqtt.Client(socket.gethostname())
    client.on_message = on_message
    logging.info("connecting to broker %s", broker_address)
    client.connect(broker_address, port=broker_port, keepalive=broker_keepalive)
    for domain in domains:
        topic = f"wireguard/{domain}/+"
        logging.info(f"Subscribing to topic {topic}")
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
    logging.debug("Got message %s from MTQQ", message)
    domain = re.search(r"/.*ffmuc_(\w+)/", message.topic).group(1)
    logging.debug("Found domain %s", domain)
    client = WireGuardClient(
        public_key=str(message.payload.decode("utf-8")),
        domain=domain,
        remove=False,
    )
    logging.info(f"Received node create message for key {client.public_key}")
    # TODO(ruairi): Verify return type here.
    print(link_handler(client))
