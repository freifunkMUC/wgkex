#!/usr/bin/env python3
import re

from flask import Flask, abort, jsonify, render_template, request
from flask_mqtt import Mqtt
from voluptuous import Invalid, MultipleInvalid, Required, Schema

from wgkex.config import load_config

app = Flask(__name__)
config = load_config()

app.config["MQTT_BROKER_URL"] = config.get("mqtt", {}).get("broker_url")
app.config["MQTT_BROKER_PORT"] = config.get("mqtt", {}).get("broker_port")
app.config["MQTT_USERNAME"] = config.get("mqtt", {}).get("username")
app.config["MQTT_PASSWORD"] = config.get("mqtt", {}).get("password")
app.config["MQTT_KEEPALIVE"] = config.get("mqtt", {}).get("keepalive")
app.config["MQTT_TLS_ENABLED"] = config.get("mqtt", {}).get("tls")

mqtt = Mqtt(app)

WG_PUBKEY_PATTERN = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$")


def is_valid_wg_pubkey(pubkey):
    if WG_PUBKEY_PATTERN.match(pubkey) is None:
        raise Invalid("Not a valid Wireguard public key")
    return pubkey


def is_valid_domain(domain):
    if domain not in config.get("domains"):
        raise Invalid("Not a valid domain")
    return domain


WG_KEY_EXCHANGE_SCHEMA_V1 = Schema(
    {Required("public_key"): is_valid_wg_pubkey, Required("domain"): is_valid_domain}
)


@app.route("/", methods=["GET"])
def index():
    # return templates/index.html
    return render_template("index.html")


# Keep to be compatible
@app.route("/wg-public-key/<path:key>", methods=["GET"])
def receive_public_key(key):
    if not is_valid_wg_pubkey(key):
        return jsonify({"Message": "Invalid Key"}), 400
    with open("/var/lib/wgkex/public.keys", "a") as pubkeys:
        pubkeys.write("%s\n" % key)
    return jsonify({"Message": "OK"}), 200


@app.route("/api/v1/wg/key/exchange", methods=["POST"])
def wg_key_exchange():
    try:
        data = WG_KEY_EXCHANGE_SCHEMA_V1(request.get_json(force=True))
    except MultipleInvalid as ex:
        return abort(400, jsonify({"error": {"message": str(ex)}}))

    key = data["public_key"]
    domain = data["domain"]
    # in case we want to decide here later we want to publish it only to dedicated gateways
    gateway = "all"
    print(f"wg_key_exchange: Domain: {domain}, Key:{key}")

    if config.get("pubkeys_file") != "":
        with open(config["pubkeys_file"], "a") as pubkeys:
            pubkeys.write(f"{key} {domain}\n")

    mqtt.publish(f"wireguard/{domain}/{gateway}", key)

    return jsonify({"Message": "OK"}), 200


@mqtt.on_connect()
def handle_mqtt_connect(client, userdata, flags, rc):
    print(
        "MQTT connected to {}:{}".format(
            app.config["MQTT_BROKER_URL"], app.config["MQTT_BROKER_PORT"]
        )
    )
    # mqtt.subscribe("wireguard/#")


@mqtt.on_message()
def handle_mqtt_message(client, userdata, message):
    print(f"MQTT message received on {message.topic}: {message.payload.decode()}")
