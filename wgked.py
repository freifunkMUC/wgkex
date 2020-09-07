#!/usr/bin/env python3
from flask import Flask, abort, jsonify, render_template, request
from voluptuous import All, Invalid, Length, MultipleInvalid, Required, Schema
import argparse
import base64
import json
import re
import sys
import yaml

app = Flask(__name__)

WG_PUBKEY_PATTERN = re.compile(
    "^[A-Za-z0-9+/]{42}[A|E|I|M|Q|U|Y|c|g|k|o|s|w|4|8|0]{1}=$"
)


def is_valid_wg_pubkey(pubkey):
    if WG_PUBKEY_PATTERN.match(pubkey) is None:
        raise Invalid("Not a valid Wireguard public key")
    return pubkey


def is_valid_segment(segment):
    if segment not in config.get("segments"):
        raise Invalid("Not a valid segment")
    return segment


CONFIG_SCHEMA = Schema(
    {
        Required("segments"): All([str], min=1),
        Required("pubkeys_file", default="/var/lib/wgke/public.keys"): str,
    }
)

WG_KEY_EXCHANGE_SCHEMA_V1 = Schema(
    {Required("public_key"): is_valid_wg_pubkey, Required("segment"): is_valid_segment}
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
    with open("/var/lib/wgke/public.keys", "a") as pubkeys:
        pubkeys.write("%s\n" % key)
    return jsonify({"Message": "OK"}), 200


@app.route("/api/v1/wg/key/exchange", methods=["POST"])
def wg_key_exchange():
    try:
        data = WG_KEY_EXCHANGE_SCHEMA_V1(request.get_json(force=True))
    except MultipleInvalid as ex:
        return abort(400, jsonify({"error": {"message": str(ex)}}))

    key = data["public_key"]
    segment = data["segment"]
    print(key, segment)

    with open(config["pubkeys_file"], "a") as pubkeys:
        pubkeys.write("%s %s\n" % (key, segment))

    return jsonify({"Message": "OK"}), 200


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Wireguard Key Exchange Daemon")
    parser.add_argument(
        "-c",
        "--config",
        help="Load configuration from CONFIG File",
        default="/etc/wgked.yaml",
    )
    args = parser.parse_args()

    with open(args.config, "r") as stream:
        try:
            config = CONFIG_SCHEMA(yaml.safe_load(stream))
        except MultipleInvalid as ex:
            print(f"Config file failed to validate: {ex}", file=sys.stderr)
            sys.exit(1)

    app.run(debug=True, host="::", port=5000)
