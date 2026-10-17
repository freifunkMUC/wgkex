"""Functions related to netlink manipulation for Wireguard, IPRoute and FDB on Linux."""

# See https://docs.pyroute2.org/iproute.html for a documentation of the layout of netlink responses
import errno
import hashlib
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from textwrap import wrap
from typing import Any

import pyroute2
import pyroute2.netlink
import pyroute2.netlink.exceptions

from wgkex.common import logger
from wgkex.common.utils import mac2eui64
from wgkex.worker.observability import NETLINK_OP_SECONDS, NETLINK_RECONNECTS

_DEFAULT_PEER_TIMEOUT = 1800


class _Handles:
    """Long-lived netlink sockets and cached interface indexes of one thread.

    Opening a WireGuard socket requires resolving the generic netlink family, which is about as
    expensive as the operation itself. pyroute2 sockets are not shared between threads, so every
    thread (queue processor, metrics, cleanup) gets its own handles.
    """

    def __init__(self) -> None:
        self._ipr = None
        self._wg = None
        self.ifindex: dict[str, int] = {}

    def iproute(self):
        if self._ipr is None:
            self._ipr = pyroute2.IPRoute()
        return self._ipr

    def wireguard(self):
        if self._wg is None:
            self._wg = pyroute2.WireGuard()
        return self._wg

    def link_index(self, ifname: str) -> int:
        """Returns the (cached) interface index, raises IndexError if the interface does not exist."""
        index = self.ifindex.get(ifname)
        if index is None:
            index = self.iproute().link_lookup(ifname=ifname)[0]
            self.ifindex[ifname] = index
        return index

    def close(self) -> None:
        for handle in (self._ipr, self._wg):
            if handle is not None:
                try:
                    handle.close()
                except Exception:  # noqa: BLE001, S110
                    pass
        self._ipr = None
        self._wg = None
        self.ifindex.clear()


_thread_local = threading.local()

# Errors caused by the request itself, a new socket would not help
_REQUEST_ERRORS = (ValueError, TypeError, KeyError, IndexError)


def _handles() -> _Handles:
    handles = getattr(_thread_local, "handles", None)
    if handles is None:
        handles = _thread_local.handles = _Handles()
    return handles


def reset_handles() -> None:
    """Closes the netlink sockets of the current thread, they are reopened on next use."""
    _handles().close()


def _netlink_call(op: Callable[[_Handles], Any], name: str = "other") -> Any:
    """Runs a netlink operation with the long-lived handles of this thread.

    Retries once with fresh interface indexes if an interface was recreated (ENODEV), and once
    with new sockets on socket level errors. The duration is recorded under the given name.
    """
    start = time.monotonic()
    try:
        return _netlink_call_with_retry(op)
    finally:
        NETLINK_OP_SECONDS.labels(name).observe(time.monotonic() - start)


def _netlink_call_with_retry(op: Callable[[_Handles], Any]) -> Any:
    handles = _handles()
    try:
        return op(handles)
    except pyroute2.netlink.exceptions.NetlinkDumpInterrupted:
        raise
    except pyroute2.netlink.exceptions.NetlinkError as e:
        if e.code != errno.ENODEV:
            raise
        logger.info("Interface vanished (%s), refreshing interface indexes", e)
        handles.ifindex.clear()
    except _REQUEST_ERRORS:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("Netlink socket error (%s), reconnecting", e)
        NETLINK_RECONNECTS.inc()
        handles.close()
    return op(handles)


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


def wg_flush_stale_peers(
    domain: str,
    timeout: int = _DEFAULT_PEER_TIMEOUT,
    keep: Callable[[str], bool] | None = None,
) -> list[dict]:
    """Removes stale peers.

    Arguments:
        domain: The domain to detect peers on.
        timeout: Seconds without handshake after which a peer is stale.
        keep: Optional predicate for public keys which must not be removed (e.g. recently added).

    Returns:
        The peers which we can remove.
    """
    logger.info("Searching for stale clients for %s", domain)
    stale_clients = [
        stale_client
        for stale_client in find_stale_wireguard_clients("wg-" + domain, timeout)
        if keep is None or not keep(stale_client)
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
def link_handler(client: WireGuardClient) -> dict:
    """Updates fdb, route and WireGuard peers tables for a given WireGuard peer.

    Arguments:
        client: A WireGuard peer to manipulate.
    Returns:
        The outcome of each operation.
    """
    results = {}
    # Updates WireGuard peers.
    results.update({"Wireguard": update_wireguard_peer(client)})
    logger.debug("Handling links for %s", client)
    try:
        # Updates routes to the WireGuard Peer.
        results.update({"Route": route_handler(client)})
        logger.info("Updated route for %s", client)
    except Exception as e:  # noqa: BLE001
        # TODO(ruairi): re-raise exception here.
        logger.error("Failed to update route for %s (%s)", client, e)
        results.update({"Route": e})
    # Updates WireGuard FDB.
    results.update({"Bridge FDB": bridge_fdb_handler(client)})
    logger.debug("Updated Bridge FDB for %s", client)
    return results


def bridge_fdb_handler(client: WireGuardClient) -> dict:
    """Handles updates of FDB info towards WireGuard peers.

    Note that set will remove an FDB entry if remove is set to True.

    Arguments:
        client: The WireGuard peer to update.

    Returns:
        A dict.
    """
    # TODO(ruairi): Splice this into an add_ and remove_ function.
    return _netlink_call(
        lambda h: h.iproute().fdb(
            "del" if client.remove else "append",
            ifindex=h.link_index(client.vx_interface),
            lladdr="00:00:00:00:00:00",
            dst=re.sub(r"/\d+$", "", client.lladdr),
            NDA_IFINDEX=h.link_index(client.wg_interface),
        ),
        "fdb",
    )


def update_wireguard_peer(client: WireGuardClient) -> dict:
    """Handles updates of WireGuard peers to netlink.

    Note that set will remove a peer if remove is set to True.

    Arguments:
        client: The WireGuard peer to update.

    Returns:
        A dict.
    """
    # TODO(ruairi): Splice this into an add_ and remove_ function.
    wg_peer = {
        "public_key": client.public_key,
        "allowed_ips": [client.lladdr],
        "remove": client.remove,
    }
    return _netlink_call(
        lambda h: h.wireguard().set(client.wg_interface, peer=wg_peer), "wg_set"
    )


def route_handler(client: WireGuardClient) -> dict:
    """Handles updates of routes towards WireGuard peers.

    Note that set will remove a route if remove is set to True.

    Arguments:
        client: The WireGuard peer to update.

    Returns:
        A dict.
    """
    # TODO(ruairi): Determine what Exceptions are raised by ip.route
    # TODO(ruairi): Splice this into an add_ and remove_ function.
    return _netlink_call(
        lambda h: h.iproute().route(
            "del" if client.remove else "replace",
            dst=client.lladdr,
            oif=h.link_index(client.wg_interface),
        ),
        "route",
    )


def find_stale_wireguard_clients(
    wg_interface: str, timeout: int = _DEFAULT_PEER_TIMEOUT
) -> list:
    """Fetches and returns a list of peers which have not had recent handshakes.

    Arguments:
        wg_interface: The WireGuard interface to query.
        timeout: Seconds without handshake after which a peer is stale.

    Returns:
        # A list of peers which have not recently seen a handshake.
    """
    stale_before = int((datetime.now(UTC) - timedelta(seconds=timeout)).timestamp())
    logger.info(
        "Starting search for stale wireguard peers for interface %s.", wg_interface
    )
    msgs = _netlink_call(lambda h: h.wireguard().info(wg_interface), "wg_info")
    all_peers = []
    for msg in msgs:
        peers = msg.get_attr("WGDEVICE_A_PEERS")
        if peers:
            all_peers.extend(peers)
    ret = [
        peer.get_attr("WGPEER_A_PUBLIC_KEY").decode("utf-8")
        for peer in all_peers
        if (hshk_time := peer.get_attr("WGPEER_A_LAST_HANDSHAKE_TIME")) is not None
        and hshk_time.get("tv_sec", 0) < stale_before
    ]
    return ret


def get_connected_peers(wg_interface: str) -> list[str]:
    """Fetches and returns the public keys of connected peers, i.e. which had recent handshakes.

    Arguments:
        wg_interface: The WireGuard interface to query.

    Returns:
        The public keys of peers which have recently seen a handshake.

    Raises:
        NetlinkDumpInterrupted if the interface data has changed while it was being returned by netlink
    """
    three_mins_ago_in_secs = int((datetime.now(UTC) - timedelta(minutes=3)).timestamp())
    logger.debug("Collecting connected wireguard peers for interface %s.", wg_interface)
    try:
        msgs = _netlink_call(lambda h: h.wireguard().info(wg_interface), "wg_info")
    except pyroute2.netlink.exceptions.NetlinkDumpInterrupted:
        # Normal behaviour, data has changed while it was being returned by netlink.
        # Retry once, don't catch the exception this time, and let the caller handle it.
        # See https://github.com/svinota/pyroute2/issues/874
        msgs = _netlink_call(lambda h: h.wireguard().info(wg_interface), "wg_info")

    connected = []
    for msg in msgs:
        peers = msg.get_attr("WGDEVICE_A_PEERS")
        if peers:
            for peer in peers:
                if (
                    hshk_time := peer.get_attr("WGPEER_A_LAST_HANDSHAKE_TIME")
                ) is not None and hshk_time.get("tv_sec", 0) > three_mins_ago_in_secs:
                    connected.append(
                        peer.get_attr("WGPEER_A_PUBLIC_KEY").decode("utf-8")
                    )

    logger.debug("Found %d connected peers on %s.", len(connected), wg_interface)
    return connected


def get_connected_peers_count(wg_interface: str) -> int:
    """Fetches and returns the number of connected peers, i.e. which had recent handshakes.

    Arguments:
        wg_interface: The WireGuard interface to query.

    Returns:
        The number of peers which have recently seen a handshake.

    Raises:
        NetlinkDumpInterrupted if the interface data has changed while it was being returned by netlink
    """
    return len(get_connected_peers(wg_interface))


def get_device_data(wg_interface: str) -> tuple[int, str, str]:
    """Returns the listening port, public key and local IP address.

    Arguments:
        wg_interface: The WireGuard interface to query.

    Returns:
        # The listening port, public key, and local IP address of the WireGuard interface.
    """
    logger.info("Reading data from interface %s.", wg_interface)
    msgs = _netlink_call(lambda h: h.wireguard().info(wg_interface), "wg_info")
    if len(msgs) > 1:
        logger.warning(
            "Got multiple messages from netlink, expected one. Using only first one."
        )
    info: pyroute2.netlink.nla = msgs[0]

    port = int(info.get_attr("WGDEVICE_A_LISTEN_PORT"))
    public_key = info.get_attr("WGDEVICE_A_PUBLIC_KEY").decode("ascii")

    # Get link address using IPRoute
    msgs = _netlink_call(
        lambda h: h.iproute().get_addr(index=h.link_index(wg_interface)), "get_addr"
    )
    link_address = msgs[0].get_attr("IFA_ADDRESS")

    logger.debug(
        "Interface data: port '%s', public key '%s', link address '%s",
        port,
        public_key,
        link_address,
    )

    return (port, public_key, link_address)
