#!/usr/bin/env python3
import hashlib
import re

from netlink import find_stale_wireguard_clients, link_handler, WireGuardClient

from dataclasses import dataclass
from salt.utils.network import mac2eui64
from textwrap import wrap
from typing import Dict, List

# we receive stuff from wgkex-broker
def generate_lladdr(public_key: str) -> str:
    m = hashlib.md5()

    m.update(public_key.encode("ascii") + b"\n")
    hashed_key = m.hexdigest()
    hash_as_list = wrap(hashed_key, 2)
    temp_mac = ":".join(["02"] + hash_as_list[:5])

    lladdr = re.sub("\/\d+$", "/128", mac2eui64(mac=temp_mac, prefix="fe80::/10"))
    return lladdr


def generate_interface_names(peer: WireGuardClient) -> WireGuardClient:
    peer.wg_interface = "wg-" + peer.domain
    peer.vx_interface = "vx-" + peer.domain

    return peer


def cleanup_wireguard_clients(domain: str) -> bool:
    stale_clients = find_stale_wireguard_clients("wg-" + domain)
    result = []
    for stale_client in stale_clients:
        stale_wireguard_client = WireGuardClient(
            public_key=stale_client,
            lladdr=generate_lladdr(stale_client),
            domain=domain,
            wg_interface="",
            vx_interface="",
            remove=True,
        )
        stale_wireguard_client = generate_interface_names(stale_wireguard_client)
        result = link_handler(stale_wireguard_client)
    return result


def main():
    client = WireGuardClient(
        public_key="WtGOpUZMbKRXeO/GEFRLH9xzMf/LXa9XmSwqtjT/Egs=",
        lladdr=b"",
        domain="welt",
        wg_interface="",
        vx_interface="",
        remove=False,
    )
    client.lladdr = generate_lladdr(client.public_key)

    client = generate_interface_names(client)

    print(link_handler(client))

    # print(cleanup_wireguard_clients("welt"))


if __name__ == "__main__":
    main()
