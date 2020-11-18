#!/usr/bin/env python3
import paho.mqtt.client as mqtt
from wgkex.config import load_config
import socket
import time
import re
from wgkex.worker.netlink import (
    find_stale_wireguard_clients,
    link_handler,
    WireGuardClient,
    generate_lladdr,
    generate_ifname,
)


def connect(domains: str):
    broker_address = config.get("mqtt", {}).get("broker_url")
    client = mqtt.Client(socket.gethostname())
    client.on_message = on_message
    print("connecting to broker " + broker_address)
    client.connect(broker_address)
    for domain in domains:
        print("Subscribing to topic", "wireguard/" + domain + "/+")
        client.subscribe("wireguard/" + domain + "/+")
    client.loop_forever()


def on_message(client, userdata, message):

    domain = re.search("/.*ffmuc_(\w+)/", message.topic).group(1)

    client = WireGuardClient(
        public_key=str(message.payload.decode("utf-8")),
        lladdr=b"",
        domain=domain,
        wg_interface="",
        vx_interface="",
        remove=False,
    )

    client.lladdr = generate_lladdr(client.public_key)

    client = generate_ifname(client)

    print("Received node create message for key " + client.public_key)

    print(link_handler(client))
