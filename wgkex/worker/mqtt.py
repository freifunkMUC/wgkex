#!/usr/bin/env python3
"""Process messages from MQTT."""

import paho.mqtt.client as mqtt

# TODO(ruairi): Deprecate __init__.py from config, as it masks namespace.
from wgkex.config.config import load_config
import socket
import re
from typing import Optional, Dict, Any, Union
from wgkex.common import logger
from wgkex.worker.msg_queue import q


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


def connect() -> None:
    """Connect to MQTT for the given domains.

    Argument:
        domains: The domains to connect to.
    """
    base_config = fetch_from_config("mqtt")
    broker_address = base_config.get("broker_url")
    broker_port = base_config.get("broker_port")
    broker_keepalive = base_config.get("keepalive")
    # TODO(ruairi): Move the hostname to a global variable.
    client = mqtt.Client(socket.gethostname())

    # Register handlers
    client.on_connect = on_connect
    client.on_message = on_message
    logger.info("connecting to broker %s", broker_address)

    client.connect(broker_address, port=broker_port, keepalive=broker_keepalive)
    client.loop_forever()


# The callback for when the client receives a CONNACK response from the server.
def on_connect(client: mqtt.Client, userdata: Any, flags, rc) -> None:
    """Handles MQTT connect and subscribes to topics on connect

    Arguments:
        client: the client instance for this callback.
        userdata: the private user data.
        flags: The MQTT flags.
        rc: The MQTT rc.
    """
    logger.debug("Connected with result code " + str(rc))
    domains = load_config().get("domains")

    # Subscribing in on_connect() means that if we lose the connection and
    # reconnect then subscriptions will be renewed.
    for domain in domains:
        topic = f"wireguard/{domain}/+"
        logger.info(f"Subscribing to topic {topic}")
        client.subscribe(topic)


def on_message(client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
    """Processes messages from MQTT and forwards them to netlink.

    Arguments:
        client: the client instance for this callback.
        userdata: the private user data.
        message: The MQTT message.
    """
    # TODO(ruairi): Check bounds and raise exception here.
    logger.debug("Got message %s from MTQQ", message)
    domain_prefixes = load_config().get("domain_prefixes")
    domain = None
    for domain_prefix in domain_prefixes:
        domain = re.search(r"/.*" + domain_prefix + "(\w+)/", message.topic)
        if domain:
            break
    if not domain:
        raise ValueError(
            "Could not find a match for %s on %s", repr(domain_prefixes), message.topic
        )
    # this will not work, if we have non-unique prefix stripped domains
    domain = domain.group(1)
    logger.debug("Found domain %s", domain)
    logger.info(
        f"Received create message for key {str(message.payload.decode('utf-8'))} on domain {domain} adding to queue"
    )
    q.put((domain, str(message.payload.decode("utf-8"))))
