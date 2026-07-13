"""Process messages from MQTT."""

# TODO(ruairi): Deprecate __init__.py from config, as it masks namespace.
import json
import re
import socket
import threading
import time
from typing import Any, List, Optional

import paho.mqtt.client as mqtt
import paho.mqtt.enums
import pyroute2.netlink.exceptions

from wgkex.common import logger
from wgkex.common.mqtt import (
    STATUS_DRAINING,
    TOPIC_CONNECTED_PEERS,
    TOPIC_PARKER_CONNECTED_PEERS,
    TOPIC_PARKER_WORKER_STATUS,
    TOPIC_PARKER_WORKER_WG_DATA,
    TOPIC_WG_KEY,
    TOPIC_WORKER_REBALANCE,
    TOPIC_WORKER_REMOVE_PEER,
    TOPIC_WORKER_STATUS,
    TOPIC_WORKER_WG_DATA,
    WG_KEY_ALL_WORKERS,
)
from wgkex.config.config import get_config
from wgkex.worker import drain
from wgkex.worker.msg_queue import PeerOp, q
from wgkex.worker.netlink import (
    get_connected_peers_count,
    get_device_data,
)
from wgkex.worker.rebalance import Rebalancer

_HOSTNAME = socket.gethostname()
_METRICS_SEND_INTERVAL = 60
_METRICS_SEND_INTERVAL_PARKER = 10
_ANNOUNCE_RETRY_INTERVAL = 10

# Set when active rebalancing is enabled in the configuration
_rebalancer: Rebalancer | None = None
_announce_retry_lock = threading.Lock()
_announce_retry_running = False
# Status last published while the worker was ready (None while not announced)
_status_lock = threading.Lock()
_published_status: int | None = None


def _rebalancing_enabled() -> bool:
    # Compare with True explicitly, so that mocked configs don't enable it by accident
    return get_config().load_balancing.rebalancing.enabled is True


def _status_topic() -> str:
    """The retained status topic of this worker, Parker workers use their own namespace."""
    if get_config().parker.enabled:
        return TOPIC_PARKER_WORKER_STATUS.format(worker=_HOSTNAME)
    return TOPIC_WORKER_STATUS.format(worker=_HOSTNAME)


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
    client.will_set(_status_topic(), 0, qos=1, retain=True)

    # Register handlers
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    if parker_enabled:
        client.message_callback_add("parker/wireguard/#", on_message_parker)
    else:
        client.message_callback_add("wireguard/#", on_message_wireguard)
        client.message_callback_add(
            TOPIC_WORKER_REMOVE_PEER.format(worker=_HOSTNAME, domain="+"),
            on_message_remove_peer,
        )

    global _rebalancer
    if not parker_enabled and _rebalancing_enabled():
        logger.info("Active rebalancing is enabled")
        _rebalancer = Rebalancer(
            get_config().load_balancing.rebalancing, domains, wg_interface_name
        )
        client.message_callback_add(
            TOPIC_WORKER_REBALANCE.format(worker=_HOSTNAME), on_message_rebalance
        )
    logger.info("connecting to broker %s", broker_address)

    client.username_pw_set(broker_username, broker_password)
    client.connect(broker_address, port=broker_port, keepalive=broker_keepalive)

    # Start background threads that should not be restarted on reconnect

    # Mark worker as offline on graceful shutdown, after exit_event is set
    def mark_offline_on_exit(exit_event: threading.Event):
        exit_event.wait()
        if client.is_connected():
            logger.info("Marking worker as down")
            client.publish(_status_topic(), 0, qos=1, retain=True)

    mark_offline_on_exit_thread = threading.Thread(
        target=mark_offline_on_exit, args=(exit_event,)
    )
    mark_offline_on_exit_thread.start()

    drain_cfg = get_config().drain
    drain.drain_state.configure(
        drain_cfg.file if isinstance(drain_cfg.file, str) else None
    )
    threading.Thread(
        target=drain.watch,
        args=(
            exit_event,
            lambda status: publish_status(client, status),
            (
                drain_cfg.check_interval
                if isinstance(drain_cfg.check_interval, int)
                else 5
            ),
        ),
        daemon=True,
    ).start()

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

    if _rebalancer is not None:
        threading.Thread(
            target=_rebalancer.loop, args=(exit_event,), daemon=True
        ).start()

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
    global _published_status
    with _status_lock:
        # The broker reset the status via the LWT, it is published again after the next announce
        _published_status = None


def _publish_parker_worker_data(client: mqtt.Client, own_external_name: str) -> bool:
    try:
        port, public_key, link_address = get_device_data("wg-nodes")
    except Exception as ex:
        # Don't let the exception escape into paho's network loop.
        logger.warning("WireGuard interface wg-nodes is not ready yet: %s", ex)
        return False

    data = {
        "ExternalAddress": own_external_name,
        "Port": port,
        "PublicKey": public_key,
        "LinkAddress": link_address,
    }
    client.publish(
        TOPIC_PARKER_WORKER_WG_DATA.format(worker=_HOSTNAME),
        json.dumps(data),
        qos=1,
        retain=True,
    )
    return True


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

    if parker_enabled:
        topic = "parker/wireguard/+"
        logger.info(f"Subscribing to topic {topic}")
        client.subscribe(topic)
    else:
        domains = get_config().domains
        for domain in domains:
            # Subscribing in on_connect() means that if we lose the connection and
            # reconnect then subscriptions will be renewed.
            # Keys from the v1 API are sent to all workers, keys from the v2 API only to the chosen one.
            for worker in (WG_KEY_ALL_WORKERS, _HOSTNAME):
                topic = TOPIC_WG_KEY.format(domain=domain, worker=worker)
                logger.info(f"Subscribing to topic {topic}")
                client.subscribe(topic, qos=1)
        client.subscribe(
            TOPIC_WORKER_REMOVE_PEER.format(worker=_HOSTNAME, domain="+"), qos=1
        )

    if _rebalancer is not None:
        client.subscribe(TOPIC_WORKER_REBALANCE.format(worker=_HOSTNAME))

    # Only mark the worker as online once the data of all WireGuard interfaces could be published,
    # otherwise the broker would send clients to interfaces which are not ready yet (e.g. during boot).
    if not publish_worker_data_and_status(client):
        _start_announce_retry(client)


def publish_worker_data_and_status(client: mqtt.Client) -> bool:
    """Publishes the worker data (WG pubkeys, ports, local addresses) of all domains and,
    if successful for all of them, marks the worker as online.

    Returns:
        Whether the worker was marked as online.
    """
    own_external_name = (
        get_config().external_name
        if get_config().external_name is not None
        else _HOSTNAME
    )
    ready = True
    if get_config().parker.enabled:
        if not _publish_parker_worker_data(client, own_external_name):
            return False
        domains: List[str] = []
    else:
        domains = get_config().domains
    for domain in domains:
        iface = wg_interface_name(domain)
        if not iface:
            logger.error(
                f"Could not get interface name for domain {domain}. Skipping worker data publication"
            )
            ready = False
            continue
        try:
            port, public_key, link_address = get_device_data(iface)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"WireGuard interface {iface} is not ready yet: {e}")
            ready = False
            continue
        data = {
            "ExternalAddress": own_external_name,
            "Port": port,
            "PublicKey": public_key,
            "LinkAddress": link_address,
        }
        client.publish(
            TOPIC_WORKER_WG_DATA.format(worker=_HOSTNAME, domain=domain),
            json.dumps(data),
            qos=1,
            retain=True,
        )

    if not ready:
        return False

    # Mark worker as online (or draining)
    global _published_status
    with _status_lock:
        status = drain.drain_state.status()
        client.publish(_status_topic(), status, qos=1, retain=True)
        _published_status = status
    logger.info("Published worker status %d", status)
    return True


def publish_status(client: mqtt.Client, status: int) -> bool:
    """Publishes a changed worker status (online/draining), once the worker has been announced.

    Returns:
        False if the worker is not announced yet, the status is then published by the announcement.
    """
    global _published_status
    with _status_lock:
        if _published_status is None:
            return False
        if _published_status == status:
            return True
        client.publish(_status_topic(), status, qos=1, retain=True)
        _published_status = status
    if status == STATUS_DRAINING:
        logger.warning("Worker is draining, published status %d", status)
    else:
        logger.warning("Worker stopped draining, published status %d", status)
    return True


def _start_announce_retry(client: mqtt.Client) -> None:
    """Retries publishing worker data and status in the background until it succeeds."""
    global _announce_retry_running
    with _announce_retry_lock:
        if _announce_retry_running:
            return
        _announce_retry_running = True

    def retry() -> None:
        global _announce_retry_running
        try:
            while True:
                time.sleep(_ANNOUNCE_RETRY_INTERVAL)
                if not client.is_connected():
                    # Messages published while disconnected would be lost, wait for the reconnect
                    continue
                if publish_worker_data_and_status(client):
                    return
        finally:
            with _announce_retry_lock:
                _announce_retry_running = False

    threading.Thread(target=retry, daemon=True).start()


def on_message(client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
    """Fallback handler for MQTT messages that do not match any other registered handler.

    Arguments:
        client: the client instance for this callback.
        userdata: the private user data.
        message: The MQTT message.
    """
    logger.info("Got unhandled message on %s from MQTT", message.topic)


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
    target = message.topic.rsplit("/", 1)[-1]
    if target not in (WG_KEY_ALL_WORKERS, _HOSTNAME):
        logger.debug("Ignoring key for worker %s on %s", target, message.topic)
        return

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
        f"Received create message for key {message.payload.decode('utf-8')!s} on domain {domain} adding to queue"
    )
    q.put(PeerOp(domain, str(message.payload.decode("utf-8"))))


def on_message_remove_peer(
    client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage
) -> None:
    """Removes a peer which the broker assigned to another worker.

    Arguments:
        client: the client instance for this callback.
        userdata: the private user data.
        message: The MQTT message.
    """
    try:
        _, _, domain, _ = message.topic.split("/", 3)
        interface = wg_interface_name(domain)
        public_key = message.payload.decode("utf-8")
    except ValueError as e:
        logger.error(f"Ignored invalid remove message on {message.topic}: {e}")
        return
    logger.info(
        f"Received remove message for key {public_key} on domain {domain} adding to queue"
    )
    q.put(PeerOp(interface.removeprefix("wg-"), public_key, remove=True))


def on_message_rebalance(
    client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage
) -> None:
    """Passes rebalancing hints from the broker to the rebalancer."""
    if _rebalancer is not None:
        _rebalancer.handle_instruction(message.payload)


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
        topic = TOPIC_PARKER_CONNECTED_PEERS.format(worker=_HOSTNAME)
    else:
        logger.info("Scheduling metrics task for %s, ", domain)
        topic = TOPIC_CONNECTED_PEERS.format(domain=domain, worker=_HOSTNAME)

    while not exit_event.is_set():
        try:
            if parker_enabled:
                publish_metrics_parker(client, topic)
            else:
                publish_metrics(client, topic, domain)  # type: ignore
        except Exception as e:  # noqa: BLE001
            # Don't crash the thread when an exception is encountered
            if parker_enabled:
                logger.error("Exception during publish metrics task", exc_info=e)
            else:
                logger.error(
                    f"Exception during publish metrics task for {domain}", exc_info=e
                )
        finally:
            # This drifts slightly over time, doesn't matter for us
            exit_event.wait(
                _METRICS_SEND_INTERVAL_PARKER
                if parker_enabled
                else _METRICS_SEND_INTERVAL
            )

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


def wg_interface_name(domain: str) -> str | None:
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
