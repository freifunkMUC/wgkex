"""Functions related to netlink manipulation for Wireguard, IPRoute and FDB on Linux."""

# See https://docs.pyroute2.org/iproute.html for a documentation of the layout of netlink responses
import errno
import hashlib
import ipaddress
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from textwrap import wrap
from time import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import pyroute2
import pyroute2.netlink
import pyroute2.netlink.exceptions

from wgkex.common import logger
from wgkex.common.utils import mac2eui64
from wgkex.worker.peer_state import PeerMutationCoordinator, peer_mutations

_PARKER_STALE_TIMEOUT = 300
_LEGACY_STALE_TIMEOUT = 10800
_INITIAL_HANDSHAKE_GRACE = 600
_NETLINK_DUMP_RETRIES = 1
_ABSENT_ERRNOS = {errno.ENOENT, errno.ENODEV, errno.ESRCH}


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


@dataclass
class ParkerWireGuardClient:
    """A Class representing a Project Parker WireGuard client.

    Attributes:
        public_key: The public key to use for this client.
        remove: If this is to be removed or not.
    """

    public_key: str
    range6: str
    remove: bool
    keepalive: Optional[int] = None
    previous_ranges6: Tuple[str, ...] = ()


@dataclass(frozen=True)
class StaleWireGuardPeer:
    """A validated peer selected for cleanup."""

    public_key: str
    parker_prefix: Optional[str]
    reason: str


@dataclass(frozen=True)
class OperationOutcome:
    """The explicit outcome of one netlink mutation."""

    status: str
    result: Any = None


@dataclass(frozen=True)
class PeerCleanupResult:
    """The explicit result of one stale peer cleanup attempt."""

    public_key: str
    status: str
    operations: Dict[str, OperationOutcome]
    failed_operation: Optional[str] = None
    error: Optional[str] = None


class PeerMutationError(RuntimeError):
    """A peer mutation failed after zero or more successful operations."""

    def __init__(
        self,
        operation: str,
        client: Union[WireGuardClient, ParkerWireGuardClient],
        operations: Dict[str, OperationOutcome],
        cause: Exception,
    ) -> None:
        super().__init__(f"{operation} failed for peer {client.public_key}: {cause}")
        self.operation = operation
        self.client = client
        self.operations = operations
        self.cause = cause


def wg_flush_stale_peers(
    parker: bool = False,
    domain: str = "",
    *,
    stale_timeout: Optional[float] = None,
    initial_handshake_grace: float = _INITIAL_HANDSHAKE_GRACE,
    parker_prefix_length: int = 63,
    coordinator: PeerMutationCoordinator = peer_mutations,
    clock: Callable[[], float] = time,
) -> List[PeerCleanupResult]:
    """Removes stale peers.

    Arguments:
        domain: The domain to detect peers on.

    Returns:
        The outcome for every peer selected for cleanup.
    """
    if stale_timeout is None:
        stale_timeout = _PARKER_STALE_TIMEOUT if parker else _LEGACY_STALE_TIMEOUT
    if parker:
        retry_pending_parker_routes(
            coordinator=coordinator,
            prefix_length=parker_prefix_length,
        )
        wg_interface = "wg-nodes"
        logger.info("Searching for stale clients interface=%s", wg_interface)
        stale_clients = find_stale_wireguard_clients(
            parker,
            wg_interface,
            stale_timeout=stale_timeout,
            initial_handshake_grace=initial_handshake_grace,
            parker_prefix_length=parker_prefix_length,
            coordinator=coordinator,
            clock=clock,
        )
        logger.debug("Found %s stale clients: %s", len(stale_clients), stale_clients)
        stale_wireguard_clients = [
            ParkerWireGuardClient(
                public_key=stale_client.public_key,
                range6=stale_client.parker_prefix or "",
                remove=True,
            )
            for stale_client in stale_clients
        ]
    else:
        wg_interface = "wg-" + domain
        logger.info(
            "Searching for stale clients interface=%s domain=%s",
            wg_interface,
            domain,
        )
        stale_clients = find_stale_wireguard_clients(
            parker,
            wg_interface,
            stale_timeout=stale_timeout,
            initial_handshake_grace=initial_handshake_grace,
            coordinator=coordinator,
            clock=clock,
        )
        logger.debug("Found %s stale clients: %s", len(stale_clients), stale_clients)
        stale_wireguard_clients = [
            WireGuardClient(
                public_key=stale_client.public_key,
                domain=domain,
                remove=True,
            )
            for stale_client in stale_clients
        ]

    results = []
    for client in stale_wireguard_clients:
        related_resource = (
            client.range6 if isinstance(client, ParkerWireGuardClient) else None
        )
        with coordinator.peer_lock(client.public_key, related_resource):
            if coordinator.recently_provisioned(
                client.public_key, initial_handshake_grace
            ):
                logger.info(
                    "Deferring cleanup for recently provisioned peer "
                    "interface=%s public_key=%s",
                    wg_interface,
                    client.public_key,
                )
                results.append(
                    PeerCleanupResult(
                        public_key=client.public_key,
                        status="deferred",
                        operations={},
                    )
                )
                continue
            current_stale_peers = find_stale_wireguard_clients(
                parker,
                wg_interface,
                stale_timeout=stale_timeout,
                initial_handshake_grace=initial_handshake_grace,
                parker_prefix_length=parker_prefix_length,
                coordinator=coordinator,
                clock=clock,
            )
            still_stale = any(
                stale_peer.public_key == client.public_key
                and (not parker or stale_peer.parker_prefix == related_resource)
                for stale_peer in current_stale_peers
            )
            if not still_stale:
                logger.info(
                    "Deferring cleanup after peer state changed "
                    "interface=%s public_key=%s",
                    wg_interface,
                    client.public_key,
                )
                results.append(
                    PeerCleanupResult(
                        public_key=client.public_key,
                        status="deferred",
                        operations={},
                    )
                )
                continue
            try:
                preserve_parker_route = isinstance(
                    client, ParkerWireGuardClient
                ) and parker_prefix_owned_by_other_peer(
                    wg_interface,
                    client.range6,
                    client.public_key,
                    parker_prefix_length,
                )
                operations = link_handler(
                    client, preserve_parker_route=preserve_parker_route
                )
            except PeerMutationError as error:
                logger.error(
                    "Peer cleanup failed interface=%s public_key=%s "
                    "operation=%s completed_operations=%s error=%s",
                    wg_interface,
                    client.public_key,
                    error.operation,
                    list(error.operations),
                    error.cause,
                    exc_info=error,
                )
                results.append(
                    PeerCleanupResult(
                        public_key=client.public_key,
                        status="failed",
                        operations=error.operations,
                        failed_operation=error.operation,
                        error=str(error.cause),
                    )
                )
                continue

            coordinator.forget(client.public_key)
            logger.info(
                "Peer cleanup completed interface=%s public_key=%s operations=%s",
                wg_interface,
                client.public_key,
                {name: outcome.status for name, outcome in operations.items()},
            )
            results.append(
                PeerCleanupResult(
                    public_key=client.public_key,
                    status="deleted",
                    operations=operations,
                )
            )
    return results


##################
# pyroute2 stuff #
##################


def _is_absent_error(error: Exception) -> bool:
    code = getattr(error, "code", None)
    return isinstance(code, int) and abs(code) in _ABSENT_ERRNOS


def _run_operation(
    operation: str,
    client: Union[WireGuardClient, ParkerWireGuardClient],
    handler: Callable[[Union[WireGuardClient, ParkerWireGuardClient]], Dict],
    operations: Dict[str, OperationOutcome],
) -> None:
    try:
        result = handler(client)
    except pyroute2.netlink.exceptions.NetlinkError as error:
        if client.remove and _is_absent_error(error):
            logger.info(
                "Peer cleanup dependency already absent public_key=%s "
                "operation=%s errno=%s",
                client.public_key,
                operation,
                error.code,
            )
            operations[operation] = OperationOutcome(status="already_absent")
            return
        raise PeerMutationError(operation, client, operations.copy(), error) from error
    except Exception as error:
        raise PeerMutationError(operation, client, operations.copy(), error) from error
    operations[operation] = OperationOutcome(
        status="deleted" if client.remove else "updated",
        result=result,
    )


def link_handler(
    client: Union[WireGuardClient, ParkerWireGuardClient],
    *,
    preserve_parker_route: bool = False,
) -> Dict[str, OperationOutcome]:
    """Updates fdb, route and WireGuard peers tables for a given WireGuard peer.

    Arguments:
        client: A WireGuard peer to manipulate.
    Returns:
        The outcome of each operation.
    """
    operations: Dict[str, OperationOutcome] = {}
    logger.debug("Handling links for %s", client)
    if client.remove:
        # Keep the peer discoverable until every dependent object is gone.
        if isinstance(client, WireGuardClient):
            _run_operation("bridge_fdb", client, bridge_fdb_handler, operations)
        if preserve_parker_route:
            logger.info(
                "Preserving Parker route owned by another peer "
                "public_key=%s prefix=%s",
                client.public_key,
                client.range6,
            )
            operations["route"] = OperationOutcome(status="preserved_shared")
        else:
            _run_operation("route", client, route_handler, operations)
        _run_operation("wireguard", client, update_wireguard_peer, operations)
    else:
        _run_operation("wireguard", client, update_wireguard_peer, operations)
        _run_operation("route", client, route_handler, operations)
        if isinstance(client, ParkerWireGuardClient):
            for previous_range6 in client.previous_ranges6:
                if previous_range6 == client.range6:
                    continue
                previous_client = ParkerWireGuardClient(
                    public_key=client.public_key,
                    range6=previous_range6,
                    remove=True,
                )
                _run_operation(
                    f"old_route:{previous_range6}",
                    previous_client,
                    route_handler,
                    operations,
                )
        if isinstance(client, WireGuardClient):
            _run_operation("bridge_fdb", client, bridge_fdb_handler, operations)
    return operations


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


def update_wireguard_peer(
    client: Union[WireGuardClient, ParkerWireGuardClient],
) -> Dict:
    """Handles updates of WireGuard peers to netlink.

    Note that set will remove a peer if remove is set to True.

    Arguments:
        client: The WireGuard peer to update.

    Returns:
        A dict.
    """
    # TODO(ruairi): Splice this into an add_ and remove_ function.
    with pyroute2.WireGuard() as wg:
        if isinstance(client, WireGuardClient):
            wg_peer = {
                "public_key": client.public_key,
                "allowed_ips": [client.lladdr],
                "remove": client.remove,
            }
            wg_interface = client.wg_interface
        elif isinstance(client, ParkerWireGuardClient):
            wg_peer = {
                "public_key": client.public_key,
                "allowed_ips": [client.range6],
                "remove": client.remove,
            }
            if client.keepalive is not None:
                wg_peer["persistent_keepalive"] = client.keepalive
            wg_interface = "wg-nodes"  # TODO make interface name configurable

        return wg.set(interface=wg_interface, peer=wg_peer)


def route_handler(client: Union[WireGuardClient, ParkerWireGuardClient]) -> Dict:
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
        if isinstance(client, WireGuardClient):
            dst = client.lladdr
            oif = ip.link_lookup(client.wg_interface)[0]

        elif isinstance(client, ParkerWireGuardClient):
            dst = client.range6
            oif = ip.link_lookup("wg-nodes")[0]  # TODO make interface name configurable

        result = ip.route(
            "del" if client.remove else "replace",
            dst=dst,
            oif=oif,
        )
        return dict(result) if isinstance(result, dict) else {"result": result}


def _wireguard_info(
    wg: pyroute2.WireGuard,
    wg_interface: str,
    retries: int = _NETLINK_DUMP_RETRIES,
) -> List:
    for attempt in range(retries + 1):
        try:
            return wg.info(wg_interface)
        except pyroute2.netlink.exceptions.NetlinkDumpInterrupted:
            if attempt == retries:
                raise
            logger.warning(
                "WireGuard netlink dump interrupted; retrying "
                "interface=%s attempt=%s max_attempts=%s",
                wg_interface,
                attempt + 1,
                retries + 1,
            )
    raise AssertionError("unreachable")


def _peer_public_key(peer: pyroute2.netlink.nla) -> str:
    public_key = peer.get_attr("WGPEER_A_PUBLIC_KEY")
    if isinstance(public_key, bytes):
        public_key = public_key.decode("ascii")
    if not isinstance(public_key, str) or not public_key:
        raise ValueError("missing or invalid public key")
    return public_key


def _last_handshake_seconds(peer: pyroute2.netlink.nla) -> Optional[int]:
    handshake = peer.get_attr("WGPEER_A_LAST_HANDSHAKE_TIME")
    if handshake is None:
        return None
    if not isinstance(handshake, dict):
        raise ValueError("invalid last handshake attribute")
    seconds = handshake.get("tv_sec")
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
        raise ValueError("invalid last handshake seconds")
    return seconds


def _parker_prefix(peer: pyroute2.netlink.nla, prefix_length: int) -> str:
    allowed_ips = peer.get_attr("WGPEER_A_ALLOWEDIPS")
    if not isinstance(allowed_ips, (list, tuple)):
        raise ValueError("missing or invalid allowed IPs")

    matches = set()
    for allowed_ip in allowed_ips:
        address = allowed_ip.get("addr") if isinstance(allowed_ip, dict) else None
        if not isinstance(address, str):
            continue
        try:
            network = ipaddress.ip_network(address, strict=False)
        except ValueError:
            continue
        if network.version == 6 and network.prefixlen == prefix_length:
            matches.add(str(network))
    if len(matches) != 1:
        raise ValueError(
            f"expected one IPv6 /{prefix_length} Parker prefix, "
            f"found {len(matches)}"
        )
    return matches.pop()


def parker_prefix_owned_by_other_peer(
    wg_interface: str,
    parker_prefix: str,
    public_key: str,
    prefix_length: int,
) -> bool:
    """Return whether another current peer owns a Parker prefix."""
    with pyroute2.WireGuard() as wg:
        messages = _wireguard_info(wg, wg_interface)
    for message in messages:
        peers = message.get_attr("WGDEVICE_A_PEERS")
        if not isinstance(peers, (list, tuple)):
            continue
        for peer in peers:
            try:
                peer_prefix = _parker_prefix(peer, prefix_length)
            except (ValueError, AttributeError):
                continue
            if peer_prefix != parker_prefix:
                continue
            try:
                peer_public_key = _peer_public_key(peer)
            except (UnicodeDecodeError, ValueError, AttributeError):
                logger.warning(
                    "Preserving Parker route with ambiguous owner "
                    "interface=%s prefix=%s",
                    wg_interface,
                    parker_prefix,
                )
                return True
            if peer_public_key != public_key:
                return True
    return False


def retry_pending_parker_routes(
    *,
    coordinator: PeerMutationCoordinator,
    prefix_length: int,
    wg_interface: str = "wg-nodes",
) -> None:
    """Retry old Parker route deletions left by partial queue updates."""
    for prefix in coordinator.pending_parker_routes():
        with coordinator.peer_lock("", prefix):
            try:
                has_owner = parker_prefix_owned_by_other_peer(
                    wg_interface, prefix, "", prefix_length
                )
            except pyroute2.netlink.exceptions.NetlinkDumpInterrupted as error:
                logger.warning(
                    "Pending Parker route ownership check failed "
                    "interface=%s prefix=%s error=%s",
                    wg_interface,
                    prefix,
                    error,
                )
                continue
            if has_owner:
                logger.info(
                    "Pending Parker route is owned again; preserving "
                    "interface=%s prefix=%s",
                    wg_interface,
                    prefix,
                )
                coordinator.forget_pending_parker_route(prefix)
                continue

            operations: Dict[str, OperationOutcome] = {}
            client = ParkerWireGuardClient(
                public_key="pending-route-cleanup",
                range6=prefix,
                remove=True,
            )
            try:
                _run_operation("route", client, route_handler, operations)
            except PeerMutationError as error:
                logger.error(
                    "Pending Parker route cleanup failed "
                    "interface=%s prefix=%s error=%s",
                    wg_interface,
                    prefix,
                    error.cause,
                    exc_info=error,
                )
                continue
            coordinator.forget_pending_parker_route(prefix)
            logger.info(
                "Pending Parker route cleanup completed "
                "interface=%s prefix=%s status=%s",
                wg_interface,
                prefix,
                operations["route"].status,
            )


def get_parker_prefixes_for_peer(
    wg_interface: str,
    public_key: str,
    prefix_length: int,
) -> List[str]:
    """Return all assigned Parker prefixes currently owned by a peer."""
    with pyroute2.WireGuard() as wg:
        messages = _wireguard_info(wg, wg_interface)
    prefixes = set()
    for message in messages:
        peers = message.get_attr("WGDEVICE_A_PEERS")
        if not isinstance(peers, (list, tuple)):
            continue
        for peer in peers:
            try:
                if _peer_public_key(peer) != public_key:
                    continue
                allowed_ips = peer.get_attr("WGPEER_A_ALLOWEDIPS")
            except (UnicodeDecodeError, ValueError, AttributeError):
                continue
            if not isinstance(allowed_ips, (list, tuple)):
                continue
            for allowed_ip in allowed_ips:
                address = (
                    allowed_ip.get("addr") if isinstance(allowed_ip, dict) else None
                )
                if not isinstance(address, str):
                    continue
                try:
                    network = ipaddress.ip_network(address, strict=False)
                except ValueError:
                    continue
                if network.version == 6 and network.prefixlen == prefix_length:
                    prefixes.add(str(network))
    return sorted(prefixes)


def find_stale_wireguard_clients(
    parker: bool,
    wg_interface: str,
    *,
    stale_timeout: Optional[float] = None,
    initial_handshake_grace: float = _INITIAL_HANDSHAKE_GRACE,
    parker_prefix_length: int = 63,
    coordinator: PeerMutationCoordinator = peer_mutations,
    clock: Callable[[], float] = time,
) -> List[StaleWireGuardPeer]:
    """Fetches and returns a list of peers which have not had recent handshakes.

    Arguments:
        wg_interface: The WireGuard interface to query.

    Returns:
        # A list of peers which have not recently seen a handshake.
    """
    if stale_timeout is None:
        stale_timeout = _PARKER_STALE_TIMEOUT if parker else _LEGACY_STALE_TIMEOUT
    now = clock()
    stale_before = now - stale_timeout
    logger.info(
        "Starting search for stale wireguard peers for interface %s.", wg_interface
    )
    with pyroute2.WireGuard() as wg:
        all_peers = []
        msgs = _wireguard_info(wg, wg_interface)
        logger.debug("Got infos for stale peers: %s.", msgs)
        for msg in msgs:
            peers = msg.get_attr("WGDEVICE_A_PEERS")
            if peers is not None and not isinstance(peers, (list, tuple)):
                logger.warning(
                    "Skipping malformed WireGuard peer list interface=%s",
                    wg_interface,
                )
                continue
            if peers:
                all_peers.extend(peers)

        stale_peers = []
        for peer_index, peer in enumerate(all_peers):
            try:
                public_key = _peer_public_key(peer)
                last_handshake = _last_handshake_seconds(peer)
                if coordinator.recently_provisioned(
                    public_key, initial_handshake_grace
                ):
                    continue
                if last_handshake in (None, 0):
                    if coordinator.defer_never_handshaked(
                        public_key, initial_handshake_grace
                    ):
                        continue
                    reason = "never_handshaked"
                elif last_handshake <= stale_before:
                    reason = "stale_handshake"
                else:
                    continue
                parker_prefix = (
                    _parker_prefix(peer, parker_prefix_length) if parker else None
                )
            except (UnicodeDecodeError, ValueError, AttributeError) as error:
                logger.warning(
                    "Skipping malformed WireGuard peer interface=%s "
                    "peer_index=%s reason=%s",
                    wg_interface,
                    peer_index,
                    error,
                )
                continue
            stale_peers.append(
                StaleWireGuardPeer(
                    public_key=public_key,
                    parker_prefix=parker_prefix,
                    reason=reason,
                )
            )
        return stale_peers


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
        msgs = _wireguard_info(wg, wg_interface)

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
