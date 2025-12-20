"""Functions related to netlink manipulation for Wireguard, IPRoute and FDB on Linux."""

# See https://docs.pyroute2.org/iproute.html for a documentation of the layout of netlink responses
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from textwrap import wrap
from typing import Dict, List, Optional, Tuple

import pyroute2
import pyroute2.netlink
import pyroute2.netlink.exceptions

from wgkex.common import logger
from wgkex.common.utils import mac2eui64
from wgkex.config import config

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
    logger.info("Searching for stale clients for %s", domain)
    stale_clients = [
        stale_client for stale_client in find_stale_wireguard_clients("wg-" + domain)
    ]
    logger.debug("Found %s stale clients: %s", len(stale_clients), stale_clients)
    stale_wireguard_clients = [
        WireGuardClient(public_key=stale_client, domain=domain, remove=True)
        for stale_client in stale_clients
    ]
    logger.debug("Found stale WireGuard clients: %s", stale_wireguard_clients)
    logger.info("Processing clients.")
    link_handled = [
        link_handler(stale_client) for stale_client in stale_wireguard_clients
    ]
    logger.debug("Handled the following clients: %s", link_handled)
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
    logger.debug("Handling links for %s", client)
    try:
        # Updates routes to the WireGuard Peer.
        results.update({"Route": route_handler(client)})
        logger.info("Updated route for %s", client)
    except Exception as e:
        # TODO(ruairi): re-raise exception here.
        logger.error("Failed to update route for %s (%s)", client, e)
        results.update({"Route": e})
    # Updates WireGuard FDB.
    results.update({"Bridge FDB": bridge_fdb_handler(client)})
    logger.debug("Updated Bridge FDB for %s", client)
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
            NDA_IFINDEX=ip.link_lookup(ifname=client.wg_interface)[0],
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
            "del" if client.remove else "replace",
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
    logger.info(
        "Starting search for stale wireguard peers for interface %s.", wg_interface
    )
    
    # Get whitelist from configuration
    whitelist = config.get_config().key_whitelist or []
    if whitelist:
        logger.info("Key whitelist active with %d keys", len(whitelist))
    
    with pyroute2.WireGuard() as wg:
        all_peers = []
        msgs = wg.info(wg_interface)
        logger.debug("Got infos for stale peers: %s.", msgs)
        for msg in msgs:
            peers = msg.get_attr("WGDEVICE_A_PEERS")
            logger.debug("Got clients: %s.", peers)
            if peers:
                all_peers.extend(peers)
        ret = [
            peer.get_attr("WGPEER_A_PUBLIC_KEY").decode("utf-8")
            for peer in all_peers
            if (hshk_time := peer.get_attr("WGPEER_A_LAST_HANDSHAKE_TIME")) is not None
            and hshk_time.get("tv_sec", int()) < three_hrs_in_secs
            and peer.get_attr("WGPEER_A_PUBLIC_KEY").decode("utf-8") not in whitelist
        ]
        return ret


def get_connected_peers_count(wg_interface: str) -> int:
    """Fetches and returns the number of connected peers, i.e. which had recent handshakes.

    Arguments:
        wg_interface: The WireGuard interface to query.

    Returns:
        The number of peers which have recently seen a handshake.

    Raises:
        NetlinkDumpInterrupted if the interface data has changed while it was being returned by netlink
    """
    three_mins_ago_in_secs = int((datetime.now() - timedelta(minutes=3)).timestamp())
    logger.info("Counting connected wireguard peers for interface %s.", wg_interface)
    with pyroute2.WireGuard() as wg:
        try:
            msgs = wg.info(wg_interface)
        except pyroute2.netlink.exceptions.NetlinkDumpInterrupted:
            # Normal behaviour, data has changed while it was being returned by netlink.
            # Retry once, don't catch the exception this time, and let the caller handle it.
            # See https://github.com/svinota/pyroute2/issues/874
            msgs = wg.info(wg_interface)

        logger.debug("Got infos for connected peers: %s.", msgs)
        count = 0
        for msg in msgs:
            peers = msg.get_attr("WGDEVICE_A_PEERS")
            logger.debug("Got clients: %s.", peers)
            if peers:
                for peer in peers:
                    if (
                        hshk_time := peer.get_attr("WGPEER_A_LAST_HANDSHAKE_TIME")
                    ) is not None and hshk_time.get(
                        "tv_sec", int()
                    ) > three_mins_ago_in_secs:
                        count += 1

        return count


def get_device_data(wg_interface: str) -> Tuple[int, str, str]:
    """Returns the listening port, public key and local IP address.

    Arguments:
        wg_interface: The WireGuard interface to query.

    Returns:
        # The listening port, public key, and local IP address of the WireGuard interface.
    """
    logger.info("Reading data from interface %s.", wg_interface)
    with pyroute2.WireGuard() as wg, pyroute2.IPRoute() as ipr:
        msgs = wg.info(wg_interface)
        logger.debug("Got infos for interface data: %s.", msgs)
        if len(msgs) > 1:
            logger.warning(
                "Got multiple messages from netlink, expected one. Using only first one."
            )
        info: pyroute2.netlink.nla = msgs[0]

        port = int(info.get_attr("WGDEVICE_A_LISTEN_PORT"))
        public_key = info.get_attr("WGDEVICE_A_PUBLIC_KEY").decode("ascii")

        # Get link address using IPRoute
        ipr_link = ipr.link_lookup(ifname=wg_interface)[0]
        msgs = ipr.get_addr(index=ipr_link)
        link_address = msgs[0].get_attr("IFA_ADDRESS")

        logger.debug(
            "Interface data: port '%s', public key '%s', link address '%s",
            port,
            public_key,
            link_address,
        )

        return (port, public_key, link_address)
