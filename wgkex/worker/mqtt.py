#!/usr/bin/env python3
"""Process messages from MQTT."""

# TODO(ruairi): Deprecate __init__.py from config, as it masks namespace.
import json
import re
import socket
import threading
from typing import Any, List, Optional

import paho.mqtt.enums
import paho.mqtt.client as mqtt
import pyroute2.netlink.exceptions

from wgkex.common import logger
from wgkex.common.mqtt import MQTTTopics
from wgkex.config.config import get_config
from wgkex.worker.msg_queue import q
from wgkex.worker.netlink import (
    get_connected_peers_count,
    get_device_data,
)

_HOSTNAME = socket.gethostname()
_METRICS_SEND_INTERVAL = 60


def connect(exit_event: threading.Event) -> None:
    """Connect to MQTT.

    Argument:
        exit_event: A threading.Event that signals application shutdown.
    """

    parker_enabled = get_config().parker.enabled

    base_config = get_config().mqtt
    broker_address = base_config.broker_url
    broker_port = base_config.broker_port
    broker_username = base_config.username
    broker_password = base_config.password
    broker_keepalive = base_config.keepalive
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, _HOSTNAME)

    domains: List[str] = []
    if not parker_enabled:
        domains = get_config().domains

    # Register LWT to set worker status down when loosing connection
    if parker_enabled:
        topic = MQTTTopics.TOPIC_PARKER_WORKER_STATUS.format(worker=_HOSTNAME)
    else:
        topic = MQTTTopics.TOPIC_WORKER_STATUS.format(worker=_HOSTNAME)
    client.will_set(topic, 0, qos=1, retain=True)

    # Register handlers
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    if parker_enabled:
        client.message_callback_add("parker/wireguard/#", on_message_parker)
    else:
        client.message_callback_add("wireguard/#", on_message_wireguard)
    logger.info("connecting to broker %s", broker_address)

    client.username_pw_set(broker_username, broker_password)
    client.connect(broker_address, port=broker_port, keepalive=broker_keepalive)

    # Start background threads that should not be restarted on reconnect

    # Mark worker as offline on graceful shutdown, after exit_event is set
    def mark_offline_on_exit(exit_event: threading.Event):
        exit_event.wait()
        if client.is_connected():
            logger.info("Marking worker as down")
            if parker_enabled:
                topic = MQTTTopics.TOPIC_PARKER_WORKER_STATUS.format(worker=_HOSTNAME)
            else:
                topic = MQTTTopics.TOPIC_WORKER_STATUS.format(worker=_HOSTNAME)
            client.publish(topic, 0, qos=1, retain=True)

    mark_offline_on_exit_thread = threading.Thread(
        target=mark_offline_on_exit, args=(exit_event,)
    )
    mark_offline_on_exit_thread.start()

    if parker_enabled:
        # Schedule repeated metrics publishing
        metrics_thread = threading.Thread(
            target=publish_metrics_loop,
            args=(exit_event, client, None, parker_enabled),
        )
        metrics_thread.start()
    else:
        for domain in domains:
            # Schedule repeated metrics publishing
            metrics_thread = threading.Thread(
                target=publish_metrics_loop,
                args=(exit_event, client, domain, parker_enabled),
            )
            metrics_thread.start()

    client.loop_forever()


def on_disconnect(client: mqtt.Client, userdata: Any, rc):
    """Handles MQTT disconnect and logs the event

    Expected signature for MQTT v3.1.1 and v3.1 is:
        disconnect_callback(client, userdata, rc)

    and for MQTT v5.0:
        disconnect_callback(client, userdata, reasonCode, properties)

    Arguments:
        client:     the client instance for this callback
        userdata:   the private user data as set in Client() or userdata_set()
        rc:         the disconnection result
                    The rc parameter indicates the disconnection state. If
                    MQTT_ERR_SUCCESS (0), the callback was called in response to
                    a disconnect() call. If any other value the disconnection
                    was unexpected, such as might be caused by a network error.
    """
    logger.debug("Disconnected with result code " + str(rc))


# The callback for when the client receives a CONNACK response from the server.
def on_connect(client: mqtt.Client, userdata: Any, flags, rc) -> None:
    """Handles MQTT connect and subscribes to topics on connect

    Arguments:
        client: the client instance for this callback.
        userdata: the private user data.
        flags: The MQTT flags.
        rc: The MQTT rc.
    """
    match rc:
        case paho.mqtt.enums.ConnackCode.CONNACK_ACCEPTED:
            logger.debug(
                "MQTT successfully connected to %s:%s",
                client.host,
                client.port,
            )
        case _:
            logger.error(
                "MQTT connection to %s:%s failed with return code %s (%s)",
                client.host,
                client.port,
                rc,
                paho.mqtt.enums.ConnackCode(rc).name,
            )
            return

    parker_enabled = get_config().parker.enabled

    own_external_name = (
        get_config().external_name
        if get_config().external_name is not None
        else _HOSTNAME
    )

    if parker_enabled:
        topic = "parker/wireguard/+"
        logger.info(f"Subscribing to topic {topic}")
        client.subscribe(topic)

        # Publish worker data (WG pubkeys, ports, local addresses)
        (port, public_key, link_address) = get_device_data("wg-nodes")
        data = {
            "ExternalAddress": own_external_name,
            "Port": port,
            "PublicKey": public_key,
            "LinkAddress": link_address,
        }
        client.publish(
            MQTTTopics.TOPIC_PARKER_WORKER_WG_DATA.format(worker=_HOSTNAME),
            json.dumps(data),
            qos=1,
            retain=True,
        )

    else:
        domains = get_config().domains

        for domain in domains:
            # Subscribing in on_connect() means that if we lose the connection and
            # reconnect then subscriptions will be renewed.
            topic = f"wireguard/{domain}/+"
            logger.info(f"Subscribing to topic {topic}")
            client.subscribe(topic)

        for domain in domains:
            # Publish worker data (WG pubkeys, ports, local addresses)
            iface = wg_interface_name(domain)
            if iface:
                (port, public_key, link_address) = get_device_data(iface)
                data = {
                    "ExternalAddress": own_external_name,
                    "Port": port,
                    "PublicKey": public_key,
                    "LinkAddress": link_address,
                }
                client.publish(
                    MQTTTopics.TOPIC_WORKER_WG_DATA.format(
                        worker=_HOSTNAME, domain=domain
                    ),
                    json.dumps(data),
                    qos=1,
                    retain=True,
                )
            else:
                logger.error(
                    f"Could not get interface name for domain {domain}. Skipping worker data publication"
                )

    # Mark worker as online
    if parker_enabled:
        topic = MQTTTopics.TOPIC_PARKER_WORKER_STATUS.format(worker=_HOSTNAME)
    else:
        topic = MQTTTopics.TOPIC_WORKER_STATUS.format(worker=_HOSTNAME)
    client.publish(topic, 1, qos=1, retain=True)


def on_message(client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
    """Fallback handler for MQTT messages that do not match any other registered handler.

    Arguments:
        client: the client instance for this callback.
        userdata: the private user data.
        message: The MQTT message.
    """
    logger.info("Got unhandled message on %s from MQTT", message.topic)
    return


def on_message_wireguard(
    client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage
) -> None:
    """Processes messages from MQTT and forwards them to netlink.

    Arguments:
        client: the client instance for this callback.
        userdata: the private user data.
        message: The MQTT message.
    """
    # TODO(ruairi): Check bounds and raise exception here.
    logger.debug("Got message on %s from MQTT", message.topic)

    domain_prefixes = get_config().domain_prefixes
    domain = None
    for domain_prefix in domain_prefixes:
        domain = re.search(r".*/" + domain_prefix + r"(\w+)/", message.topic)
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


def on_message_parker(
    client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage
) -> None:
    """Processes messages from MQTT and forwards them to netlink.

    Arguments:
        client: the client instance for this callback.
        userdata: the private user data.
        message: The MQTT message.
    """
    # TODO: ensure payload is a dict mathing a TBD shared data class
    q.put(str(message.payload.decode("utf-8")))


def publish_metrics_loop(
    exit_event: threading.Event,
    client: mqtt.Client,
    domain: Optional[str],
    parker_enabled: bool = False,
) -> None:
    """Continuously send metrics every METRICS_SEND_INTERVAL seconds for this gateway and the given domain."""

    if not parker_enabled and domain is None:
        raise ValueError(
            "Domain must be passed to publish_metrics_loop if Parker is not enabled"
        )

    if parker_enabled:
        logger.info("Scheduling interface metrics task, ")
        topic = MQTTTopics.TOPIC_PARKER_CONNECTED_PEERS.format(worker=_HOSTNAME)
    else:
        logger.info("Scheduling metrics task for %s, ", domain)
        topic = MQTTTopics.TOPIC_CONNECTED_PEERS.format(worker=_HOSTNAME, domain=domain)

    while not exit_event.is_set():
        try:
            if parker_enabled:
                publish_metrics_parker(client, topic)
            else:
                publish_metrics(client, topic, domain)  # type: ignore
        except Exception as e:
            # Don't crash the thread when an exception is encountered
            if parker_enabled:
                logger.error("Exception during publish metrics task", exc_info=e)
            else:
                logger.error(
                    f"Exception during publish metrics task for {domain}", exc_info=e
                )
        finally:
            # This drifts slightly over time, doesn't matter for us
            exit_event.wait(_METRICS_SEND_INTERVAL)

    # Set peers metric to -1 to mark worker as offline
    # Use QoS 1 (at least once) to make sure the broker notices
    client.publish(topic, -1, qos=1, retain=True)


def publish_metrics(client: mqtt.Client, topic: str, domain: str) -> None:
    """Publish metrics for this gateway and the given domain.

    The metrics currently only consist of the number of connected peers.
    """
    logger.debug(f"Publishing metrics for domain {domain}")
    iface = wg_interface_name(domain)
    if not iface:
        logger.error(
            f"Could not get interface name for domain {domain}. Skipping metrics publication"
        )
        return

    try:
        peer_count = get_connected_peers_count(iface)
    except pyroute2.netlink.exceptions.NetlinkDumpInterrupted:
        # Handle gracefully, don't update metrics
        logger.info(
            f"Caught NetlinkDumpInterrupted exception while collecting metrics for domain {domain}"
        )
        return

    # Publish metrics, retain it at MQTT broker so a restarted wgkex broker has metrics right away
    client.publish(topic, peer_count, retain=True)


def publish_metrics_parker(client: mqtt.Client, topic: str) -> None:
    """Publish metrics for this gateway and the given domain.

    The metrics currently only consist of the number of connected peers.
    """
    logger.debug("Publishing interface metrics")
    iface = "wg-nodes"

    try:
        peer_count = get_connected_peers_count(iface)
    except pyroute2.netlink.exceptions.NetlinkDumpInterrupted:
        # Handle gracefully, don't update metrics
        logger.info(
            "Caught NetlinkDumpInterrupted exception while collecting interface metrics"
        )
        return

    # Publish metrics, retain it at MQTT broker so a restarted wgkex broker has metrics right away
    client.publish(topic, peer_count, retain=True)


def wg_interface_name(domain: str) -> Optional[str]:
    """Calculates the WireGuard interface name for a domain"""
    domain_prefixes = get_config().domain_prefixes
    cleaned_domain = None
    for prefix in domain_prefixes:
        try:
            cleaned_domain = domain.split(prefix)[1]
        except IndexError:
            continue
        break
    if not cleaned_domain:
        raise ValueError(f"Could not find a match for {domain_prefixes} on {domain}")
    # this will not work, if we have non-unique prefix stripped domains
    return f"wg-{cleaned_domain}"
