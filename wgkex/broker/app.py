"""wgkex broker"""

import dataclasses
import json
import re
import threading
import time
from typing import Any

import paho.mqtt.client as mqtt_client
from flask import Flask, Response, render_template, request
from flask.app import Flask as Flask_app
from flask_mqtt import Mqtt
from waitress import serve

from wgkex.broker.metrics import WorkerMetricsCollection
from wgkex.common import logger
from wgkex.common.mqtt import (
    TOPIC_WORKER_REBALANCE,
    TOPIC_WORKER_STATUS,
    TOPIC_WORKER_WG_DATA,
)
from wgkex.common.utils import is_valid_domain
from wgkex.config import config

WG_PUBKEY_PATTERN = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$")


@dataclasses.dataclass
class KeyExchange:
    """A key exchange message.

    Attributes:
        public_key: The public key for this exchange.
        domain: The domain for this exchange.
    """

    public_key: str
    domain: str

    @classmethod
    def from_dict(cls, msg: dict) -> "KeyExchange":
        """Creates a new KeyExchange message from dict.

        Arguments:
            msg: The message to convert.
        Returns:
            A KeyExchange object.
        """
        public_key = is_valid_wg_pubkey(msg.get("public_key"))
        domain = str(msg.get("domain"))
        if not is_valid_domain(domain):
            raise ValueError(f"Domain {domain} not in configured domains.")
        return cls(public_key=public_key, domain=domain)


def _fetch_app_config() -> Flask_app:
    """Creates the Flask app from configuration.

    Returns:
        A created Flask app.
    """
    app = Flask(__name__)
    mqtt_cfg = config.get_config().mqtt
    app.config["MQTT_BROKER_URL"] = mqtt_cfg.broker_url
    app.config["MQTT_BROKER_PORT"] = mqtt_cfg.broker_port
    app.config["MQTT_USERNAME"] = mqtt_cfg.username
    app.config["MQTT_PASSWORD"] = mqtt_cfg.password
    app.config["MQTT_KEEPALIVE"] = mqtt_cfg.keepalive
    app.config["MQTT_TLS_ENABLED"] = mqtt_cfg.tls
    return app


app = _fetch_app_config()
mqtt = Mqtt(app)
_load_balancing_cfg = config.get_config().load_balancing
worker_metrics = WorkerMetricsCollection(
    pending_ttl=_load_balancing_cfg.pending_assignment_ttl,
    pending_settle_time=_load_balancing_cfg.pending_assignment_settle_time,
    metrics_max_age=_load_balancing_cfg.metrics_max_age,
)
worker_data: dict[tuple[str, str], dict] = {}


@app.route("/", methods=["GET"])
def index() -> str:
    """Returns main page"""
    return render_template("index.html")


@app.route("/api/v1/wg/key/exchange", methods=["POST"])
def wg_api_v1_key_exchange() -> tuple[Response | dict, int]:
    """Retrieves a new key and validates.
    Returns:
        Status message.
    """
    try:
        data = KeyExchange.from_dict(request.get_json(force=True))
    except Exception as ex:  # noqa: BLE001
        logger.error(
            "Exception occurred in /api/v1/wg/key/exchange: %s", ex, exc_info=True
        )
        return {
            "error": {
                "message": "An internal error has occurred. Please try again later."
            }
        }, 400

    key = data.public_key
    domain = data.domain
    # in case we want to decide here later we want to publish it only to dedicated gateways
    gateway = "all"
    logger.info(f"wg_api_v1_key_exchange: Domain: {domain}, Key:{key}")

    mqtt.publish(f"wireguard/{domain}/{gateway}", key, qos=1)
    return {"Message": "OK"}, 200


@app.route("/api/v2/wg/key/exchange", methods=["POST"])
def wg_api_v2_key_exchange() -> tuple[Response | dict, int]:
    """Retrieves a new key, validates it and responds with a worker/gateway the client should connect to.

    Returns:
        Status message, Endpoint with address/domain, port pubic key and link address.
    """
    try:
        data = KeyExchange.from_dict(request.get_json(force=True))
    except Exception as ex:  # noqa: BLE001
        logger.error(
            "Exception occurred in /api/v2/wg/key/exchange: %s", ex, exc_info=True
        )
        return {
            "error": {
                "message": "An internal error has occurred. Please try again later."
            }
        }, 400

    key = data.public_key
    domain = data.domain
    # in case we want to decide here later we want to publish it only to dedicated gateways
    gateway = "all"
    logger.info(f"wg_api_v2_key_exchange: Domain: {domain}, Key:{key}")

    mqtt.publish(f"wireguard/{domain}/{gateway}", key, qos=1)

    # Only consider workers whose endpoint data we know, so we can always answer with a valid endpoint
    best_worker, diff, current_peers = worker_metrics.get_best_worker(
        domain, eligible=lambda worker: (worker, domain) in worker_data
    )
    if best_worker is None:
        logger.warning(f"No worker online for domain {domain}")
        return {
            "error": {
                "message": "no gateway online for this domain, please check the domain value and try again later"
            }
        }, 400

    # Count the peer for this worker until the worker metrics reflect it
    worker_metrics.record_assignment(best_worker, domain, key)
    logger.debug(
        f"Chose worker {best_worker} with {current_peers} effective peers ({diff:.1f} to target)"
    )

    w_data = worker_data.get((best_worker, domain), None)
    if w_data is None:
        logger.error(f"Couldn't get worker endpoint data for {best_worker}/{domain}")
        return {"error": {"message": "could not get gateway data"}}, 500

    endpoint = {
        "Address": w_data.get("ExternalAddress"),
        "Port": str(w_data.get("Port")),
        "AllowedIPs": [w_data.get("LinkAddress")],
        "PublicKey": w_data.get("PublicKey"),
    }

    return {"Endpoint": endpoint}, 200


@mqtt.on_connect()
def handle_mqtt_connect(
    client: mqtt_client.Client, userdata: bytes, flags: Any, rc: Any
) -> None:
    """Prints status of connect message."""
    # TODO(ruairi): Clarify current usage of this function.
    logger.debug(
        "MQTT connected to {}:{}".format(
            app.config["MQTT_BROKER_URL"], app.config["MQTT_BROKER_PORT"]
        )
    )
    mqtt.subscribe("wireguard-metrics/#")
    mqtt.subscribe(TOPIC_WORKER_STATUS.format(worker="+"))
    mqtt.subscribe(TOPIC_WORKER_WG_DATA.format(worker="+", domain="+"))


@mqtt.on_topic("wireguard-metrics/#")
def handle_mqtt_message_metrics(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Processes published metrics from workers."""
    logger.debug(
        f"MQTT message received on {message.topic}: {_decode_payload(message.payload)}"
    )
    try:
        _, domain, worker, metric = message.topic.split("/", 3)
    except ValueError:
        logger.error(f"Ignored MQTT message on unexpected topic {message.topic}")
        return
    if not is_valid_domain(domain):
        logger.error(f"Domain {domain} not in configured domains")
        return

    if not worker or not metric:
        logger.error("Ignored MQTT message with empty worker or metrics label")
        return

    try:
        data = int(message.payload)
    except ValueError:
        logger.error(
            f"Ignored invalid metric on {message.topic}: {_decode_payload(message.payload)}"
        )
        return

    logger.debug(f"Update worker metrics: {metric} on {worker}/{domain} = {data}")
    worker_metrics.update(worker, domain, metric, data)


@mqtt.on_topic(TOPIC_WORKER_STATUS.format(worker="+"))
def handle_mqtt_message_status(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Processes status messages from workers."""
    _, worker, _ = message.topic.split("/", 2)

    try:
        status = int(message.payload)
    except ValueError:
        logger.error(
            f"Ignored invalid status on {message.topic}: {_decode_payload(message.payload)}"
        )
        return
    if status < 1 and worker_metrics.get(worker).is_online():
        logger.warning(f"Marking worker as offline: {worker}")
        worker_metrics.set_offline(worker)
    elif status >= 1 and not worker_metrics.get(worker).is_online():
        logger.warning(f"Marking worker as online: {worker}")
        worker_metrics.set_online(worker)


@mqtt.on_topic(TOPIC_WORKER_WG_DATA.format(worker="+", domain="+"))
def handle_mqtt_message_data(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Processes data messages from workers.

    Stores them in a local dict"""
    _, worker, domain, _ = message.topic.split("/", 3)
    if not is_valid_domain(domain):
        logger.error(f"Domain {domain} not in configured domains.")
        return

    try:
        data = json.loads(message.payload)
    except ValueError:
        logger.error(
            "Invalid worker data received for %s/%s: %s",
            worker,
            domain,
            _decode_payload(message.payload),
        )
        return
    if not isinstance(data, dict):
        logger.error("Invalid worker data received for %s/%s: %s", worker, domain, data)
        return

    logger.info("Worker data received for %s/%s: %s", worker, domain, data)
    worker_data[(worker, domain)] = data


@mqtt.on_message()
def handle_mqtt_message(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Prints message contents."""
    logger.debug(
        f"MQTT message received on {message.topic}: {_decode_payload(message.payload)}"
    )


def _decode_payload(payload: bytes) -> str:
    """Decodes an MQTT payload for logging, never raising on invalid data."""
    return payload.decode("utf-8", errors="replace")


def publish_rebalance_hints() -> None:
    """Publishes rebalancing hints to workers which have more peers than their weighted target."""
    cfg = config.get_config()
    rebalancing = cfg.load_balancing.rebalancing
    hints = worker_metrics.compute_rebalance_hints(
        domains=cfg.domains,
        tolerance=rebalancing.tolerance,
        min_excess=rebalancing.min_excess_peers,
        stable_time=rebalancing.stable_time,
    )
    for worker, hint in hints.items():
        logger.info(f"Asking worker {worker} to shed peers: {hint}")
        mqtt.publish(
            TOPIC_WORKER_REBALANCE.format(worker=worker),
            json.dumps(hint.to_dict()),
            qos=0,
            retain=False,
        )


def rebalance_loop() -> None:
    """Periodically publishes rebalancing hints."""
    interval = config.get_config().load_balancing.rebalancing.interval
    while True:
        time.sleep(interval)
        try:
            publish_rebalance_hints()
        except Exception as e:  # noqa: BLE001
            # Don't crash the thread when an exception is encountered
            logger.error("Exception during rebalancing task: %s", e, exc_info=True)


def is_valid_wg_pubkey(pubkey: str) -> str:
    """Verifies if key is a valid WireGuard public key or not.

    Arguments:
        pubkey: The key to verify.

    Raises:
        ValueError: If the Wireguard Key is invalid.

    Returns:
        The public key.
    """
    # TODO(ruairi): Refactor to return bool.
    if WG_PUBKEY_PATTERN.match(pubkey) is None:
        raise ValueError(f"Not a valid Wireguard public key: {pubkey}.")
    return pubkey


def join_host_port(host: str, port: str) -> str:
    """Concatenate a port string with a host string using a colon.
    The host may be either a hostname, IPv4 or IPv6 address.
    An IPv6 address as host will be automatically encapsulated in square brackets.

    Returns:
        The joined host:port string
    """
    if host.find(":") >= 0:
        return "[" + host + "]:" + port
    return host + ":" + port


if __name__ == "__main__":
    listen_host = None
    listen_port = None

    listen_config = config.get_config().broker_listen
    if listen_config is not None:
        listen_host = listen_config.host
        listen_port = listen_config.port

    if config.get_config().load_balancing.rebalancing.enabled:
        logger.info("Active rebalancing is enabled")
        threading.Thread(target=rebalance_loop, daemon=True).start()

    serve(app, host=listen_host, port=listen_port)
