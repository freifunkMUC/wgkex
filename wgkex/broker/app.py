#!/usr/bin/env python3
"""wgkex broker"""

import dataclasses
import ipaddress
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import paho.mqtt.client as mqtt_client
from flask import Flask, Response, render_template, request
from flask.app import Flask as Flask_app
from flask_mqtt import Mqtt
from waitress import serve

from wgkex.broker.metrics import WorkerMetricsCollection
from wgkex.broker.signer import sign_response
from wgkex.common import logger
from wgkex.common.mqtt import (
    CONNECTED_PEERS_METRIC,
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
    @dataclasses.dataclass
    class ParkerQuery:
        v6mtu: int
        pubkey: str  # client WG pubkey, base64 encoded
        nonce: str

        def __init__(self, v6mtu: int, pubkey: str, nonce: str) -> None:
            self.v6mtu = v6mtu
            self.pubkey = is_valid_wg_pubkey(pubkey)
            self.nonce = nonce

        @classmethod
        def from_dict(cls, data: dict[str, Any]) -> "ParkerQuery":
            """Creates a new Query object from dict."""
            v6mtu: int = int(data.get("v6mtu", 1280))
            pubkey: str = is_valid_wg_pubkey(data.get("pubkey", ""))
            nonce: str = data.get("nonce", "")
            return cls(v6mtu=v6mtu, pubkey=pubkey, nonce=nonce)

    @dataclasses.dataclass
    class ParkerResponse:
        nonce: str
        time: int  # current time as unix timestamp in seconds datetime.now(tz=timezone.utc).timestamp()
        # NodeInfo data
        # type NodeInfo struct {
        # 	ID                    *uint64            `json:"id,omitempty" etcd:"id"`
        # 	Concentrators         []ConcentratorInfo `json:"concentrators,omitempty" etcd:"-"`
        # 	ConcentratorsJSON     []byte             `json:"-" etcd:"concentrators"`
        # 	MTU                   *uint64            `json:"mtu,omitempty" etcd:"mtu"`
        # 	Retry                 *uint64            `json:"retry,omitempty" etcd:"retry"`
        # 	WGKeepalive           *uint64            `json:"wg_keepalive,omitempty" etcd:"wg_keepalive"`
        # 	Range4                *string            `json:"range4,omitempty" etcd:"range4"`
        # 	Range6                *string            `json:"range6,omitempty" etcd:"range6"`
        # 	Address4              *string            `json:"address4,omitempty" etcd:"address4"`
        # 	Address6              *string            `json:"address6,omitempty" etcd:"address6"`
        # 	SelectedConcentrators *string            `json:"-" etcd:"selected_concentrators"`
        # }

        # type ConcentratorInfo struct {
        # 	Address4 string `json:"address4"`
        # 	Address6 string `json:"address6"`
        # 	Endpoint string `json:"endpoint"`
        # 	PubKey   string `json:"pubkey"`
        # 	ID       uint32 `json:"id"`
        # }

        id: str
        mtu: int
        address6: str
        concentrators: List[Dict[str, str | int]]
        # selected_concentrators: This value contains a space separated list of the concentrator ids to
        # include in the config response. If it is empty or not set, it will
        # default to return all concentrators. Due to the implementation it is
        # currently only possible to use concentrator ids between 1 and 64.
        selected_concentrators: str
        range6: str  # TODO take from IPAM
        xlat_range6: str  # FFMUC addition
        range4: str = "10.80.99.0/22"  # always the same with 464XLAT
        address4: str = "10.80.99.1"
        wg_keepalive: int = 25
        retry: int = 120

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

    # TODO: fetch IPv6 /63 from Netbox, first /64 for the node's client network, second /64 for 464XLAT
    # push both to gateways via MQTT, so that they configure the routing for it

    full_range6 = get_range6(req_data.pubkey)
    if full_range6 is None:
        return {
            "error": {
                "message": "No IPv6 range available for this public key. Please try again later."
            }
        }, 500
    ranges = full_range6.subnets(new_prefix=64)
    range6 = ranges.__next__()
    xlat_range6 = ranges.__next__()

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
        concentrators=[
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


def get_range6(pubkey: str) -> Optional[ipaddress.IPv6Network]:
    """Returns the IPv6 range for a node with a given public key."""
    ranges: Dict[str, str] = {}
    parent_prefix = ipaddress.IPv6Network("2001:db8:ed0::/56")  # Default parent prefix
    try:
        with open("/var/local/wgkex/broker/ipv6_ranges.json", "r") as f:
            json_content = json.load(f)
            ranges = json_content.get("ranges", {})
            parent_prefix = ipaddress.IPv6Network(
                json_content.get("parent_prefix", parent_prefix)
            )
    except FileNotFoundError:
        os.makedirs("/var/local/wgkex/broker", exist_ok=True)
    except json.JSONDecodeError:
        pass

    range = ranges.get(pubkey, None)
    if range is None or not ipaddress.IPv6Network(range).subnet_of(parent_prefix):
        parsed_ranges = [
            ipaddress.IPv6Network(rg)
            for rg in ranges.values()
            if ipaddress.IPv6Network(rg).subnet_of(parent_prefix)
        ]  # Filter out any ranges that are not subnets of the parent prefix

        prefixes = parent_prefix.subnets(new_prefix=63)
        next(prefixes)  # skip first
        for candidate in prefixes:
            if candidate not in parsed_ranges:
                range = candidate
                break
        if range is None:
            logger.error(f"No IPv6 range available for public key {pubkey}.")
            return None
        else:
            logger.info(
                f"No existing IPv6 range found for public key {pubkey}, assigning {range}"
            )

        ranges[pubkey] = str(range)
        with open("/var/local/wgkex/broker/ipv6_ranges.json", "w") as f:
            json.dump({"parent_prefix": str(parent_prefix), "ranges": ranges}, f)

    return ipaddress.IPv6Network(range)


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
