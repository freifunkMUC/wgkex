#!/usr/bin/env python3
"""wgkex broker"""
import dataclasses
import json
import re
from typing import Dict, Tuple, Any

import paho.mqtt.client as mqtt_client
from flask import Flask, render_template, request, Response
from flask.app import Flask as Flask_app
from flask_mqtt import Mqtt

from waitress import serve
from wgkex.config import config
from wgkex.common import logger
from wgkex.common.utils import is_valid_domain
from wgkex.broker.metrics import WorkerMetricsCollection
from wgkex.common.mqtt import (
    CONNECTED_PEERS_METRIC,
    TOPIC_WORKER_STATUS,
    TOPIC_WORKER_WG_DATA,
)

WG_PUBKEY_PATTERN = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$")

_BANNED_KEYS = list()
_BANNED_CLIENTS = list()


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
worker_metrics = WorkerMetricsCollection()
worker_data: Dict[Tuple[str, str], Dict] = {}


@app.route("/", methods=["GET"])
def index() -> str:
    """Returns main page"""
    return render_template("index.html")


@app.route("/api/v1/wg/key/exchange", methods=["POST"])
def wg_api_v1_key_exchange() -> Tuple[Response | Dict, int]:
    """Retrieves a new key and validates.
    Returns:
        Status message.
    """
    try:
        data = KeyExchange.from_dict(request.get_json(force=True))
    except Exception as ex:
        return {"error": {"message": str(ex)}}, 400

    key = data.public_key
    if key in _BANNED_KEYS:
        logger.info(
            f"wg_key_exchange: Got bad key from %s (%s)", request.remote_addr, data
        )
        return abort(403, jsonify({"error": {"Key is banned."}}))
    if request.remote_addr in _BANNED_CLIENTS:
        logger.info(
            f"wg_key_exchange: Got key from banned client: %s (%s)",
            request.remote_addr,
            data,
        )
        return abort(403, jsonify({"error": {"Client is banned."}}))

    domain = data.domain
    # in case we want to decide here later we want to publish it only to dedicated gateways
    gateway = "all"
    logger.info(f"wg_api_v1_key_exchange: Domain: {domain}, Key:{key}")

    mqtt.publish(f"wireguard/{domain}/{gateway}", key)
    return {"Message": "OK"}, 200


@app.route("/api/v2/wg/key/exchange", methods=["POST"])
def wg_api_v2_key_exchange() -> Tuple[Response | Dict, int]:
    """Retrieves a new key, validates it and responds with a worker/gateway the client should connect to.

    Returns:
        Status message, Endpoint with address/domain, port pubic key and link address.
    """
    try:
        data = KeyExchange.from_dict(request.get_json(force=True))
    except Exception as ex:
        return {"error": {"message": str(ex)}}, 400

    key = data.public_key
    domain = data.domain
    # in case we want to decide here later we want to publish it only to dedicated gateways
    gateway = "all"
    logger.info(f"wg_api_v2_key_exchange: Domain: {domain}, Key:{key}")

    mqtt.publish(f"wireguard/{domain}/{gateway}", key)

    best_worker, diff, current_peers = worker_metrics.get_best_worker(domain)
    if best_worker is None:
        logger.warning(f"No worker online for domain {domain}")
        return {
            "error": {
                "message": "no gateway online for this domain, please check the domain value and try again later"
            }
        }, 400

    # Update number of peers locally to interpolate data between MQTT updates from the worker
    # TODO fix data race
    current_peers_domain = (
        worker_metrics.get(best_worker)
        .get_domain_metrics(domain)
        .get(CONNECTED_PEERS_METRIC, 0)
    )
    worker_metrics.update(
        best_worker, domain, CONNECTED_PEERS_METRIC, current_peers_domain + 1
    )
    logger.debug(
        f"Chose worker {best_worker} with {current_peers} connected clients ({diff})"
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


@app.route("/api/v1/wg/key/block", methods=["POST"])
def wg_key_block() -> Tuple[str, int]:
    """Blocks a key from being send onwards to MQTT.

    Message format is as follows:
    {
      'client_literal': '',
      'key_literal': '',
    }

    key_literal is a literal key.
    client_literal is a string representing the source IP (v4,v6) of a client banned from sending keys.

    Returns:
        Status message.
    """
    try:
        data = request.get_json(force=True)
    except TypeError as ex:
        return abort(400, jsonify({"error": {"message": str(ex)}}))
    key = data.get("key_literal")
    client = data.get("client_literal")
    if key:
        _BANNED_KEYS.append(key)
    if client:
        _BANNED_CLIENTS.append(client)
    jsonify({"Message": "OK"}), 200


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
        f"MQTT message received on {message.topic}: {message.payload.decode()}"
    )
    _, domain, worker, metric = message.topic.split("/", 3)
    if not is_valid_domain(domain):
        logger.error(f"Domain {domain} not in configured domains")
        return

    if not worker or not metric:
        logger.error("Ignored MQTT message with empty worker or metrics label")
        return

    data = int(message.payload)

    logger.info(f"Update worker metrics: {metric} on {worker}/{domain} = {data}")
    worker_metrics.update(worker, domain, metric, data)


@mqtt.on_topic(TOPIC_WORKER_STATUS.format(worker="+"))
def handle_mqtt_message_status(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Processes status messages from workers."""
    _, worker, _ = message.topic.split("/", 2)

    status = int(message.payload)
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

    data = json.loads(message.payload)
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
        f"MQTT message received on {message.topic}: {message.payload.decode()}"
    )


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

    serve(app, host=listen_host, port=listen_port)
