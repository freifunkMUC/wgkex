#!/usr/bin/env python3
"""Process messages from MQTT."""

# TODO(ruairi): Deprecate __init__.py from config, as it masks namespace.
import socket
import re
from typing import Any

import paho.mqtt.client as mqtt

from wgkex.common import logger
from wgkex.config.config import get_config
from wgkex.worker.msg_queue import q
from wgkex.worker.netlink import link_handler, WireGuardClient


def connect() -> None:
    """Connect to MQTT."""
    base_config = get_config().mqtt
    broker_address = base_config.broker_url
    broker_port = base_config.broker_port
    broker_keepalive = base_config.keepalive
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
    domains = get_config().domains

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
    domain_prefixes = get_config().domain_prefixes
    domain = None
    for domain_prefix in domain_prefixes:
        domain = re.search(r"/.*" + domain_prefix + "(\w+)/", message.topic)
        if domain:
            break
    if not domain:
        raise ValueError(
            f"Could not find a match for {domain_prefixes} on {message.topic}"
        )
    # this will not work, if we have non-unique prefix stripped domains
    domain = domain.group(1)
    logger.debug("Found domain %s", domain)
    logger.info(
        f"Received create message for key {str(message.payload.decode('utf-8'))} on domain {domain} adding to queue"
    )
    q.put((domain, str(message.payload.decode("utf-8"))))
