"""Functions related to netlink manipulation for Wireguard, IPRoute and FDB on Linux."""
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from textwrap import wrap
from typing import Dict, List

import pyroute2

from wgkex.common.utils import mac2eui64

_PERSISTENT_KEEPALIVE_SECONDS = 15
_PEER_TIMEOUT_HOURS = 3


@dataclass
class WireGuardClient:
    """A Class representing a WireGuard client.

    Attributes:
        public_key: The public key to use for this client.
        domain: The domain for this client.
        remove: If this is to be removed or not.
    """

    public_key: str
    domain: str
    remove: bool

    @property
    def lladdr(self) -> str:
        """Compute the X for an (IPv6) Link-Local address.

        Returns:
            IPv6 Link-Local address of the WireGuard peer.
        """
        pub_key_hash = hashlib.md5()
        pub_key_hash.update(self.public_key.encode("ascii") + b"\n")
        hashed_key = pub_key_hash.hexdigest()
        hash_as_list = wrap(hashed_key, 2)
        current_mac_addr = ":".join(["02"] + hash_as_list[:5])

        return re.sub(
            r"/\d+$", "/128", mac2eui64(mac=current_mac_addr, prefix="fe80::/10")
        )

    @property
    def vx_interface(self) -> str:
        """Returns the name of the VxLAN interface associated with this lladdr."""
        return f"vx-{self.domain}"

    @property
    def wg_interface(self) -> str:
        """Returns the WireGuard peer interface."""
        return f"wg-{self.domain}"


def wg_flush_stale_peers(domain: str) -> List[Dict]:
    """Removes stale peers.

    Arguments:
        domain: The domain to detect peers on.

    Returns:
        The peers which we can remove.
    """
    stale_clients = [
        stale_client for stale_client in find_stale_wireguard_clients("wg-" + domain)
    ]
    stale_wireguard_clients = [
        WireGuardClient(public_key=stale_client, domain=domain, remove=True)
        for stale_client in stale_clients
    ]
    link_handled = [
        link_handler(stale_client) for stale_client in stale_wireguard_clients
    ]
    return link_handled


# pyroute2 stuff
def link_handler(client: WireGuardClient) -> Dict:
    """Updates fdb, route and WireGuard peers tables for a given WireGuard peer.

    Arguments:
        client: A WireGuard peer to manipulate.
    Returns:
        The outcome of each operation.
    """
    results = dict()
    # Updates WireGuard peers.
    results.update({"Wireguard": update_wireguard_peer(client)})
    try:
        # Updates routes to the WireGuard Peer.
        results.update({"Route": route_handler(client)})
    except Exception as e:
        # TODO(ruairi): re-raise exception here.
        results.update({"Route": e})
    # Updates WireGuard FDB.
    results.update({"Bridge FDB": bridge_fdb_handler(client)})
    return results


def bridge_fdb_handler(client: WireGuardClient) -> Dict:
    """Handles updates of FDB info towards WireGuard peers.

    Note that set will remove an FDB entry if remove is set to True.

    Arguments:
        client: The WireGuard peer to update.

    Returns:
        A dict.
    """
    # TODO(ruairi): Splice this into an add_ and remove_ function.
    with pyroute2.IPRoute() as ip:
        return ip.fdb(
            "del" if client.remove else "append",
            ifindex=ip.link_lookup(ifname=client.vx_interface)[0],
            lladdr="00:00:00:00:00:00",
            dst=re.sub(r"/\d+$", "", client.lladdr),
        )


def update_wireguard_peer(client: WireGuardClient) -> Dict:
    """Handles updates of WireGuard peers to netlink.

    Note that set will remove a peer if remove is set to True.

    Arguments:
        client: The WireGuard peer to update.

    Returns:
        A dict.
    """
    # TODO(ruairi): Splice this into an add_ and remove_ function.
    with pyroute2.WireGuard() as wg:
        wg_peer = {
            "public_key": client.public_key,
            "persistent_keepalive": _PERSISTENT_KEEPALIVE_SECONDS,
            "allowed_ips": [client.lladdr],
            "remove": client.remove,
        }
        return wg.set(client.wg_interface, peer=wg_peer)


def route_handler(client: WireGuardClient) -> Dict:
    """Handles updates of routes towards WireGuard peers.

    Note that set will remove a route if remove is set to True.

    Arguments:
        client: The WireGuard peer to update.

    Returns:
        A dict.
    """
    # TODO(ruairi): Determine what Exceptions are raised by ip.route
    # TODO(ruairi): Splice this into an add_ and remove_ function.
    with pyroute2.IPRoute() as ip:
        return ip.route(
            "del" if client.remove else "add",
            dst=client.lladdr,
            oif=ip.link_lookup(ifname=client.wg_interface)[0],
        )


def find_stale_wireguard_clients(wg_interface: str) -> List:
    """Fetches and returns a list of peers which have not had recent handshakes.

    Arguments:
        wg_interface: The WireGuard interface to query.

    Returns:
        # A list of peers which have not recently seen a handshake.
    """
    three_hrs_in_secs = int(
        (datetime.now() - timedelta(hours=_PEER_TIMEOUT_HOURS)).timestamp()
    )
    with pyroute2.WireGuard() as wg:
        clients = []
        infos = wg.info(wg_interface)
        for info in infos:
            clients.extend(info.WGDEVICE_A_PEERS.value)
        ret = [
            client.WGPEER_A_PUBLIC_KEY.get("value", "").decode("utf-8")
            for client in clients
            if client.WGPEER_A_LAST_HANDSHAKE_TIME.get("tv_sec", int())
            < three_hrs_in_secs
        ]
        return ret
