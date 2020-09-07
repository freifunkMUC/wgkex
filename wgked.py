#!/usr/bin/env python3
from flask import Flask, render_template, jsonify, request
import base64
import json
import re
import yaml
import argparse


app = Flask(__name__)

WG_PUBKEY_PATTERN = re.compile(
    "^[A-Za-z0-9+/]{42}[A|E|I|M|Q|U|Y|c|g|k|o|s|w|4|8|0]{1}=$"
)


def is_valid_wg_pubkey(pubkey):
    return WG_PUBKEY_PATTERN.match(pubkey) is not None


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
def receive_json_wg_public_key():
    request.get_json(force=True)
    print(request.json.keys())
    if (
        not request.json
        or not "public_key" in request.json
        or not "segment" in request.json
    ):
        return jsonify({"Message": "Missing Data"}), 400
    key = request.json["public_key"]
    segment = request.json["segment"]
    print(segment)
    if not is_valid_wg_pubkey(key):
        return jsonify({"Message": "Invalid Key"}), 400
    if not segment in SEGMENTS:
        return jsonify({"Message": "Invalid Segment"}), 400
    with open(PUBKEYS_FILE, "a") as pubkeys:
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
            cfg = yaml.safe_load(stream)
        except yaml.YAMLError as e:
            print("Error in configuration file: %s" % e)
        try:
            SEGMENTS = cfg["segments"]
        except Exception as e:
            print("Error in configuration file: segments missing")
            raise (e)
        try:
            PUBKEYS_FILE = cfg["pubkeys_file"]
        except Exception as e:
            PUBKEYS_FILE = "/var/lib/wgke/public.keys"

    app.run(debug=True, host="::", port=5000)
