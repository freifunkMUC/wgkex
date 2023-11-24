#!/usr/bin/env python3
"""wgkex broker"""
import re
import dataclasses
import logging
from typing import Tuple, Any

from flask import Flask
from flask import abort
from flask import jsonify
from flask import render_template
from flask import request
from flask.app import Flask as Flask_app
from flask_mqtt import Mqtt
import paho.mqtt.client as mqtt_client

from waitress import serve
from wgkex.config import config
from wgkex.common import logger

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
        domain = is_valid_domain(msg.get("domain"))
        return cls(public_key=public_key, domain=domain)


def _fetch_app_config() -> Flask_app:
    """Creates the Flask app from configuration.

    Returns:
        A created Flask app.
    """
    app = Flask(__name__)
    # TODO(ruairi): Refactor load_config to return Dataclass.
    mqtt_cfg = config.Config.from_dict(config.load_config()).mqtt
    app.config["MQTT_BROKER_URL"] = mqtt_cfg.broker_url
    app.config["MQTT_BROKER_PORT"] = mqtt_cfg.broker_port
    app.config["MQTT_USERNAME"] = mqtt_cfg.username
    app.config["MQTT_PASSWORD"] = mqtt_cfg.password
    app.config["MQTT_KEEPALIVE"] = mqtt_cfg.keepalive
    app.config["MQTT_TLS_ENABLED"] = mqtt_cfg.tls
    return app


app = _fetch_app_config()
mqtt = Mqtt(app)


@app.route("/", methods=["GET"])
def index() -> None:
    """Returns main page"""
    return render_template("index.html")


@app.route("/api/v1/wg/key/exchange", methods=["POST"])
def wg_key_exchange() -> Tuple[str, int]:
    """Retrieves a new key and validates.

    Returns:
        Status message.
    """
    try:
        data = KeyExchange.from_dict(request.get_json(force=True))
    except TypeError as ex:
        return abort(400, jsonify({"error": {"message": str(ex)}}))

    key = data.public_key
    domain = data.domain
    # in case we want to decide here later we want to publish it only to dedicated gateways
    gateway = "all"
    logger.info(f"wg_key_exchange: Domain: {domain}, Key:{key}")

    mqtt.publish(f"wireguard/{domain}/{gateway}", key)
    return jsonify({"Message": "OK"}), 200


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
    # mqtt.subscribe("wireguard/#")


@mqtt.on_message()
def handle_mqtt_message(
        client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Prints message contents."""
    # TODO(ruairi): Clarify current usage of this function.
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


def is_valid_domain(domain: str) -> str:
    """Verifies if the domain is configured.

    Arguments:
        domain: The domain to verify.

    Raises:
        ValueError: If the domain is not configured.

    Returns:
        The domain.
    """
    # TODO(ruairi): Refactor to return bool.
    if domain not in config.fetch_from_config("domains"):
        raise ValueError(
            f'Domains {domain} not in configured domains({config.fetch_from_config("domains")}) a valid domain'
        )
    return domain


if __name__ == "__main__":
    listen_host = None
    listen_port = None

    listen_config = config.fetch_from_config("broker_listen")
    if listen_config is not None:
        listen_host = listen_config.get("host")
        listen_port = listen_config.get("port")

    serve(app, host=listen_host, port=listen_port)
