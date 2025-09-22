#!/usr/bin/env python3
"""wgkex broker"""

import dataclasses
import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import paho.mqtt.client as mqtt_client
from flask import Flask, Response, render_template, request
from flask.app import Flask as Flask_app
from flask_mqtt import Mqtt
from waitress import serve

from wgkex.broker.ipam import ParkerIPAM
from wgkex.broker.ipam_netbox import NetboxIPAM
from wgkex.broker.ipam_json import JSONFileIPAM
from wgkex.broker.metrics import WorkerMetricsCollection
from wgkex.broker.parker import ParkerQuery, ParkerResponse
from wgkex.broker.signer import sign_response
from wgkex.common import logger
from wgkex.common.mqtt import (
    CONNECTED_PEERS_METRIC,
    TOPIC_WORKER_STATUS,
    TOPIC_WORKER_WG_DATA,
)
from wgkex.common.utils import is_valid_domain, is_valid_wg_pubkey
from wgkex.config import config


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


def _load_parker_ipam() -> Optional[ParkerIPAM]:
    if not config.get_config().parker.enabled:
        return None

    ipam: ParkerIPAM
    ipam_type = config.get_config().parker.ipam

    match ipam_type:
        case config.Parker.IPAM.JSON:
            ipam = JSONFileIPAM()
        case config.Parker.IPAM.NETBOX:
            netbox_cfg = config.get_config().netbox
            if netbox_cfg is None:
                # This should not happen due to earlier config validation
                raise Exception(
                    "Missing config for NetBox IPAM. This is also a config parser bug, please report."
                )

            ipam = NetboxIPAM(
                api_url=netbox_cfg.url,
                token=netbox_cfg.api_key,
                xlat=True,
            )
        case _:
            raise NotImplementedError(f"Invalid IPAM type {ipam_type}")

    return ipam


app = _fetch_app_config()
mqtt = Mqtt(app)
worker_metrics = WorkerMetricsCollection()
worker_data: Dict[Tuple[str, str], Dict] = {}
ipam: Optional[ParkerIPAM] = _load_parker_ipam()


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


@app.route("/api/v3/wg/key/exchange", methods=["GET"])
def wg_api_v3_key_exchange() -> Tuple[Response | Dict, int]:
    if not config.get_config().parker.enabled or ipam is None:
        return {
            "error": {"message": "Parker support is not enabled on this broker"}
        }, 400

    try:
        req_data = ParkerQuery.from_dict(request.args)
    except Exception as ex:
        logger.warning(
            "Couldn't parse client query in /api/v3/wg/key/exchange: %s",
            ex,
            exc_info=True,
        )
        return {
            "error": {
                "message": "Invalid data received. Please check your request and try again."
            }
        }, 400

    try:
        _, full_range6 = ipam.get_or_allocate_prefix(
            req_data.pubkey,
            not config.get_config().parker.xlat,
            True,
            config.get_config().parker.prefixes.ipv4.length or 0,
            config.get_config().parker.prefixes.ipv6.length,
        )
    except Exception as ex:
        logger.error(
            "Exception occurred while fetching/allocating IPv6 prefix for public key %s: %s",
            req_data.pubkey,
            ex,
            exc_info=True,
        )
        return {
            "error": {
                "message": "Internal error while allocating IP range. Please try again later."
            }
        }, 500

    if full_range6 is None:
        return {
            "error": {
                "message": "No IPv6 range available for this public key. Please try again later."
            }
        }, 500
    ranges = full_range6.subnets(new_prefix=64)
    range6 = next(ranges)
    xlat_range6 = next(ranges)

    gateway: str = "all"

    mqtt_data = {
        "PublicKey": req_data.pubkey,
        "Range6": str(full_range6),
        "Keepalive": None,  # TODO make configurable
    }

    logger.info(f"wg_api_v3_key_exchange: Key:{req_data.pubkey}")
    mqtt.publish(f"parker/wireguard/{gateway}", json.dumps(mqtt_data).encode("utf-8"))

    response = ParkerResponse(
        nonce=req_data.nonce,
        time=int(datetime.now(tz=timezone.utc).timestamp()),
        id=req_data.pubkey,
        mtu=min(req_data.v6mtu, 1375),
        range6=str(range6),
        xlat_range6=str(xlat_range6),
        address6=str(range6.network_address + 1),
        selected_concentrators="1",
        concentrators=[  # TODO fetch real concentrator data from worker status
            {
                "address4": "10.0.0.1",
                "address6": "fe80::28f:a7ff:fec6:7530",
                "endpoint": f"{config.get_config().external_name}:40000",
                "pubkey": "4WAyZBpHcyRE5+L4ApV+jjWgj4q1o3CrCQ3NjclXfV4=",
                "id": 1,
            }
        ],
    )

    data = json.dumps(dataclasses.asdict(response)).encode("utf-8") + "\n".encode(
        "utf-8"
    )
    if config.get_config().broker_signing_key is None:
        logger.error(
            "Parker is enabled, but no broker_signing_key is set in the config file. Can't respond to key exchange."
        )
        return {
            "error": {"message": "Internal signature error. Please try again later."}
        }, 500

    signature: bytes = sign_response(data)

    full_response: bytes = data + signature

    return (
        Response(
            response=full_response,
            mimetype="text/plain",
        ),
        200,
    )


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
