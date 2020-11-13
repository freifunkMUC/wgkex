import re

from pyroute2 import WireGuard, IPRoute
from pyroute2.netlink.rtnl import ndmsg
from typing import Dict, List
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class WireGuardClient:
    """WireGuardClient describes complete configuration for a specific WireGuard client

    Attributes:
        public_key: WireGuard Public key
        domain: Domain Name of the WireGuard peer
        lladdr: IPv6 lladdr of the WireGuard peer
        wg_interface: Name of the WireGuard interface this peer will use
        vx_interface: Name of the VXLAN interface we set a route for the lladdr to
        remove: Are we removing this peer or not?
    """

    public_key: str
    domain: str
    lladdr: bytes
    wg_interface: str
    vx_interface: str
    remove: bool


# pyroute2 stuff
def link_handler(client: WireGuardClient) -> Dict:
    results = {}

    results.update({"Wireguard": wireguard_handler(client)})
    try:
        results.update({"Route": route_handler(client)})
    except Exception as e:
        results.update({"Route": e})
    results.update({"Bridge FDB": bridge_fdb_handler(client)})

    return results


def bridge_fdb_handler(client: WireGuardClient) -> Dict:
    ip = IPRoute()

    action = "append"
    if client.remove:
        action = "del"

    return ip.fdb(
        action,
        ifindex=ip.link_lookup(ifname=client.vx_interface)[0],
        lladdr="00:00:00:00:00:00",
        dst=re.sub("\/\d+$", "", client.lladdr),
    )


def wireguard_handler(client: WireGuardClient) -> Dict:
    wg = WireGuard()

    wg_peer = {
        "public_key": client.public_key,
        "persistent_keepalive": 15,
        "allowed_ips": [client.lladdr],
        "remove": client.remove,
    }

    return wg.set(client.wg_interface, peer=wg_peer)


def route_handler(client: WireGuardClient) -> Dict:
    ip = IPRoute()

    action = "add"
    if client.remove:
        action = "del"

    return ip.route(
        action,
        dst=client.lladdr,
        oif=ip.link_lookup(ifname=client.wg_interface)[0],
    )


def find_stale_wireguard_clients(wg_interface: str) -> List:
    wg = WireGuard()

    clients = wg.info(wg_interface)[0].WGDEVICE_A_PEERS.value

    threeHoursAgo = (datetime.now() - timedelta(hours=3)).timestamp()

    stale_clients = []
    for client in clients:
        latest_handshake = client.WGPEER_A_LAST_HANDSHAKE_TIME["tv_sec"]
        if latest_handshake < int(threeHoursAgo):
            stale_clients.append(client.WGPEER_A_PUBLIC_KEY["value"].decode("utf-8"))

    return stale_clients
