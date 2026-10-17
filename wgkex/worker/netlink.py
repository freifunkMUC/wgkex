"""Functions related to netlink manipulation for Wireguard, IPRoute and FDB on Linux."""

# See https://docs.pyroute2.org/iproute.html for a documentation of the layout of netlink responses
import errno
import hashlib
import ipaddress
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
from wgkex.worker.peer_state import PeerMutationCoordinator, peer_mutations

_DEFAULT_PEER_TIMEOUT = 1800
_PARKER_PEER_TIMEOUT = 300
_INITIAL_HANDSHAKE_GRACE = 600
_PARKER_PREFIX_LENGTH = 63
_PARKER_INTERFACE = "wg-nodes"  # TODO make interface name configurable
_NETLINK_DUMP_RETRIES = 1
_ABSENT_ERRNOS = {errno.ENOENT, errno.ENODEV, errno.ESRCH}


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
    keepalive: int | None = None
    previous_ranges6: tuple[str, ...] = ()


@dataclass(frozen=True)
class StaleWireGuardPeer:
    """A validated Parker peer selected for cleanup."""

    public_key: str
    parker_prefix: str | None
    reason: str


@dataclass(frozen=True)
class OperationOutcome:
    """The explicit outcome of one netlink mutation."""

    status: str
    result: Any = None


@dataclass(frozen=True)
class PeerCleanupResult:
    """The explicit result of one stale Parker peer cleanup attempt."""

    public_key: str
    status: str
    operations: dict[str, OperationOutcome]
    failed_operation: str | None = None
    error: str | None = None


class PeerMutationError(RuntimeError):
    """A peer mutation failed after zero or more successful operations."""

    def __init__(
        self,
        operation: str,
        client: "WireGuardClient | ParkerWireGuardClient",
        operations: dict[str, OperationOutcome],
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
    timeout: float | None = None,
    keep: Callable[[str], bool] | None = None,
    *,
    initial_handshake_grace: float = _INITIAL_HANDSHAKE_GRACE,
    parker_prefix_length: int = _PARKER_PREFIX_LENGTH,
    coordinator: PeerMutationCoordinator = peer_mutations,
) -> list:
    """Removes stale peers.

    Arguments:
        parker: Whether to clean up the Parker interface instead of a legacy domain.
        domain: The domain to detect peers on (legacy only).
        timeout: Seconds without handshake after which a peer is stale.
        keep: Optional predicate for public keys which must not be removed (e.g. recently added).
        initial_handshake_grace: Parker only, see flush_stale_parker_peers.
        parker_prefix_length: Parker only, the prefix length of the assigned node prefixes.
        coordinator: Parker only, serializes cleanup with queue updates of the same peer.

    Returns:
        The handled peers: link_handler results (legacy) or PeerCleanupResults (Parker).
    """
    if parker:
        return flush_stale_parker_peers(
            stale_timeout=_PARKER_PEER_TIMEOUT if timeout is None else timeout,
            keep=keep,
            initial_handshake_grace=initial_handshake_grace,
            parker_prefix_length=parker_prefix_length,
            coordinator=coordinator,
        )
    if timeout is None:
        timeout = _DEFAULT_PEER_TIMEOUT
    logger.info("Searching for stale clients for %s", domain)
    stale_clients = [
        stale_client
        for (stale_client, _) in find_stale_wireguard_clients(
            False, "wg-" + domain, timeout
        )
        if keep is None or not keep(stale_client)
    ]
    logger.debug("Found %s stale clients: %s", len(stale_clients), stale_clients)
    stale_wireguard_clients = [
        WireGuardClient(public_key=stale_client, domain=domain, remove=True)
        for stale_client in stale_clients
    ]
    logger.info("Processing stale WireGuard clients.")
    link_handled = [
        link_handler(stale_client) for stale_client in stale_wireguard_clients
    ]
    logger.debug("Handled the following stale clients: %s", link_handled)
    return link_handled


def flush_stale_parker_peers(
    stale_timeout: float = _PARKER_PEER_TIMEOUT,
    keep: Callable[[str], bool] | None = None,
    *,
    initial_handshake_grace: float = _INITIAL_HANDSHAKE_GRACE,
    parker_prefix_length: int = _PARKER_PREFIX_LENGTH,
    coordinator: PeerMutationCoordinator = peer_mutations,
    clock: Callable[[], float] = time.time,
) -> list[PeerCleanupResult]:
    """Removes stale Parker peers and their prefix routes.

    Never-handshaked peers are kept for initial_handshake_grace after their last queue update
    (or after worker start). Each candidate is re-checked while holding the per-peer lock shared
    with the queue processor, so a concurrent update is never undone. The prefix route is kept if
    another current peer owns the prefix.

    Returns:
        The outcome for every peer selected for cleanup.
    """
    retry_pending_parker_routes(
        coordinator=coordinator, prefix_length=parker_prefix_length
    )
    wg_interface = _PARKER_INTERFACE
    logger.info("Searching for stale clients interface=%s", wg_interface)

    def find() -> list[StaleWireGuardPeer]:
        return [
            peer
            for peer in find_stale_parker_peers(
                wg_interface,
                stale_timeout=stale_timeout,
                initial_handshake_grace=initial_handshake_grace,
                parker_prefix_length=parker_prefix_length,
                coordinator=coordinator,
                clock=clock,
            )
            if keep is None or not keep(peer.public_key)
        ]

    stale_peers = find()
    logger.debug("Found %s stale clients: %s", len(stale_peers), stale_peers)

    results = []
    for stale_peer in stale_peers:
        client = ParkerWireGuardClient(
            public_key=stale_peer.public_key,
            range6=stale_peer.parker_prefix or "",
            remove=True,
        )
        with coordinator.peer_lock(client.public_key, client.range6):
            still_stale = not coordinator.recently_provisioned(
                client.public_key, initial_handshake_grace
            ) and any(
                peer.public_key == client.public_key
                and peer.parker_prefix == client.range6
                for peer in find()
            )
            if not still_stale:
                logger.info(
                    "Deferring cleanup after peer state changed interface=%s public_key=%s",
                    wg_interface,
                    client.public_key,
                )
                results.append(
                    PeerCleanupResult(
                        public_key=client.public_key, status="deferred", operations={}
                    )
                )
                continue
            try:
                preserve_route = parker_prefix_owned_by_other_peer(
                    wg_interface,
                    client.range6,
                    client.public_key,
                    parker_prefix_length,
                )
                operations = parker_link_handler(
                    client, preserve_parker_route=preserve_route
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


# pyroute2 stuff
##################
# pyroute2 stuff #
##################


def link_handler(client: WireGuardClient | ParkerWireGuardClient) -> dict:
    """Updates fdb, route and WireGuard peers tables for a given WireGuard peer.

    Arguments:
        client: A WireGuard peer to manipulate.
    Returns:
        The outcome of each operation.
    """
    if isinstance(client, ParkerWireGuardClient):
        return parker_link_handler(client)
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
    if isinstance(client, WireGuardClient):
        # Updates WireGuard FDB.
        results.update({"Bridge FDB": bridge_fdb_handler(client)})
        logger.debug("Updated Bridge FDB for %s", client)
    return results


def _is_absent_error(error: Exception) -> bool:
    code = getattr(error, "code", None)
    return isinstance(code, int) and abs(code) in _ABSENT_ERRNOS


def _run_operation(
    operation: str,
    client: WireGuardClient | ParkerWireGuardClient,
    handler: Callable[[Any], Any],
    operations: dict[str, OperationOutcome],
) -> None:
    """Runs one netlink mutation and records its outcome in operations.

    Raises:
        PeerMutationError: If the operation failed, unless the object to remove is already gone.
    """
    try:
        result = handler(client)
    except pyroute2.netlink.exceptions.NetlinkError as error:
        if client.remove and _is_absent_error(error):
            logger.info(
                "Peer dependency already absent public_key=%s operation=%s errno=%s",
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
        status="deleted" if client.remove else "updated", result=result
    )


def parker_link_handler(
    client: ParkerWireGuardClient, *, preserve_parker_route: bool = False
) -> dict[str, OperationOutcome]:
    """Updates the WireGuard peer and prefix route of a Parker peer.

    On removal, the route is removed before the peer (so the peer stays discoverable for a retry),
    unless preserve_parker_route is set because another peer owns the prefix. On update, routes
    of previous prefixes of the peer are removed after the new state is installed.

    Returns:
        The outcome of each operation.

    Raises:
        PeerMutationError: If an operation failed.
    """
    operations: dict[str, OperationOutcome] = {}
    logger.debug("Handling links for %s", client)
    if client.remove:
        if preserve_parker_route:
            logger.info(
                "Preserving Parker route owned by another peer public_key=%s prefix=%s",
                client.public_key,
                client.range6,
            )
            operations["route"] = OperationOutcome(status="preserved_shared")
        elif client.range6:
            _run_operation("route", client, route_handler, operations)
        _run_operation("wireguard", client, update_wireguard_peer, operations)
        return operations
    _run_operation("wireguard", client, update_wireguard_peer, operations)
    _run_operation("route", client, route_handler, operations)
    for previous_range6 in client.previous_ranges6:
        if previous_range6 == client.range6:
            continue
        _run_operation(
            f"old_route:{previous_range6}",
            ParkerWireGuardClient(
                public_key=client.public_key, range6=previous_range6, remove=True
            ),
            route_handler,
            operations,
        )
    return operations


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


def update_wireguard_peer(
    client: WireGuardClient | ParkerWireGuardClient,
) -> dict:
    """Handles updates of WireGuard peers to netlink.

    Note that set will remove a peer if remove is set to True.

    Arguments:
        client: The WireGuard peer to update.

    Returns:
        A dict.
    """
    # TODO(ruairi): Splice this into an add_ and remove_ function.
    if isinstance(client, ParkerWireGuardClient):
        wg_peer = {
            "public_key": client.public_key,
            "allowed_ips": [client.range6],
            "remove": client.remove,
        }
        if client.keepalive is not None:
            wg_peer["persistent_keepalive"] = client.keepalive
        wg_interface = "wg-nodes"  # TODO make interface name configurable
    else:
        wg_peer = {
            "public_key": client.public_key,
            "allowed_ips": [client.lladdr],
            "remove": client.remove,
        }
        wg_interface = client.wg_interface
    return _netlink_call(
        lambda h: h.wireguard().set(interface=wg_interface, peer=wg_peer), "wg_set"
    )


def route_handler(client: WireGuardClient | ParkerWireGuardClient) -> dict:
    """Handles updates of routes towards WireGuard peers.

    Note that set will remove a route if remove is set to True.

    Arguments:
        client: The WireGuard peer to update.

    Returns:
        A dict.
    """
    # TODO(ruairi): Determine what Exceptions are raised by ip.route
    # TODO(ruairi): Splice this into an add_ and remove_ function.
    if isinstance(client, ParkerWireGuardClient):
        dst = client.range6
        wg_interface = "wg-nodes"  # TODO make interface name configurable
    else:
        dst = client.lladdr
        wg_interface = client.wg_interface
    result = _netlink_call(
        lambda h: h.iproute().route(
            "del" if client.remove else "replace",
            dst=dst,
            oif=h.link_index(wg_interface),
        ),
        "route",
    )
    if isinstance(client, ParkerWireGuardClient):
        return dict(result) if isinstance(result, dict) else {"result": result}
    return result


def _wireguard_info(wg_interface: str, retries: int = _NETLINK_DUMP_RETRIES) -> list:
    for attempt in range(retries + 1):
        try:
            return _netlink_call(lambda h: h.wireguard().info(wg_interface), "wg_info")
        except pyroute2.netlink.exceptions.NetlinkDumpInterrupted:
            if attempt == retries:
                raise
            logger.warning(
                "WireGuard netlink dump interrupted; retrying interface=%s attempt=%s",
                wg_interface,
                attempt + 1,
            )
    raise AssertionError("unreachable")


def _all_peers(wg_interface: str) -> list:
    peers = []
    for msg in _wireguard_info(wg_interface):
        msg_peers = msg.get_attr("WGDEVICE_A_PEERS")
        if msg_peers is not None and not isinstance(msg_peers, (list, tuple)):
            logger.warning(
                "Skipping malformed WireGuard peer list interface=%s", wg_interface
            )
            continue
        if msg_peers:
            peers.extend(msg_peers)
    return peers


def _peer_public_key(peer: Any) -> str:
    public_key = peer.get_attr("WGPEER_A_PUBLIC_KEY")
    if isinstance(public_key, bytes):
        public_key = public_key.decode("ascii")
    if not isinstance(public_key, str) or not public_key:
        raise ValueError("missing or invalid public key")
    return public_key


def _last_handshake_seconds(peer: Any) -> int | None:
    handshake = peer.get_attr("WGPEER_A_LAST_HANDSHAKE_TIME")
    if handshake is None:
        return None
    if not isinstance(handshake, dict):
        raise ValueError("invalid last handshake attribute")  # noqa: TRY004
    seconds = handshake.get("tv_sec")
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
        raise ValueError("invalid last handshake seconds")
    return seconds


def _peer_prefixes(peer: Any, prefix_length: int) -> set[str]:
    allowed_ips = peer.get_attr("WGPEER_A_ALLOWEDIPS")
    if not isinstance(allowed_ips, (list, tuple)):
        return set()
    prefixes = set()
    for allowed_ip in allowed_ips:
        address = allowed_ip.get("addr") if isinstance(allowed_ip, dict) else None
        if not isinstance(address, str):
            continue
        try:
            network = ipaddress.ip_network(address, strict=False)
        except ValueError:
            continue
        if network.version == 6 and network.prefixlen == prefix_length:
            prefixes.add(str(network))
    return prefixes


def _parker_prefix(peer: Any, prefix_length: int) -> str:
    matches = _peer_prefixes(peer, prefix_length)
    if len(matches) != 1:
        raise ValueError(
            f"expected one IPv6 /{prefix_length} Parker prefix, found {len(matches)}"
        )
    return matches.pop()


def parker_prefix_owned_by_other_peer(
    wg_interface: str, parker_prefix: str, public_key: str, prefix_length: int
) -> bool:
    """Returns whether a current peer other than public_key owns a Parker prefix."""
    for peer in _all_peers(wg_interface):
        try:
            if parker_prefix not in _peer_prefixes(peer, prefix_length):
                continue
        except AttributeError:
            continue
        try:
            peer_public_key = _peer_public_key(peer)
        except (UnicodeDecodeError, ValueError, AttributeError):
            logger.warning(
                "Preserving Parker route with ambiguous owner interface=%s prefix=%s",
                wg_interface,
                parker_prefix,
            )
            return True
        if peer_public_key != public_key:
            return True
    return False


def get_parker_prefixes_for_peer(
    wg_interface: str, public_key: str, prefix_length: int
) -> list[str]:
    """Returns all Parker prefixes currently assigned to a peer."""
    prefixes: set[str] = set()
    for peer in _all_peers(wg_interface):
        try:
            if _peer_public_key(peer) != public_key:
                continue
            prefixes |= _peer_prefixes(peer, prefix_length)
        except (UnicodeDecodeError, ValueError, AttributeError):
            continue
    return sorted(prefixes)


def retry_pending_parker_routes(
    *,
    coordinator: PeerMutationCoordinator = peer_mutations,
    prefix_length: int = _PARKER_PREFIX_LENGTH,
    wg_interface: str = _PARKER_INTERFACE,
) -> None:
    """Retries old Parker route deletions left behind by partially failed queue updates."""
    for prefix in coordinator.pending_parker_routes():
        with coordinator.peer_lock("", prefix):
            try:
                has_owner = parker_prefix_owned_by_other_peer(
                    wg_interface, prefix, "", prefix_length
                )
            except pyroute2.netlink.exceptions.NetlinkDumpInterrupted as error:
                logger.warning(
                    "Pending Parker route ownership check failed prefix=%s error=%s",
                    prefix,
                    error,
                )
                continue
            if has_owner:
                logger.info("Pending Parker route is owned again prefix=%s", prefix)
                coordinator.forget_pending_parker_route(prefix)
                continue
            operations: dict[str, OperationOutcome] = {}
            client = ParkerWireGuardClient(
                public_key="pending-route-cleanup", range6=prefix, remove=True
            )
            try:
                _run_operation("route", client, route_handler, operations)
            except PeerMutationError as error:
                logger.error(
                    "Pending Parker route cleanup failed prefix=%s error=%s",
                    prefix,
                    error.cause,
                )
                continue
            coordinator.forget_pending_parker_route(prefix)
            logger.info(
                "Pending Parker route cleanup completed prefix=%s status=%s",
                prefix,
                operations["route"].status,
            )


def find_stale_parker_peers(
    wg_interface: str = _PARKER_INTERFACE,
    *,
    stale_timeout: float = _PARKER_PEER_TIMEOUT,
    initial_handshake_grace: float = _INITIAL_HANDSHAKE_GRACE,
    parker_prefix_length: int = _PARKER_PREFIX_LENGTH,
    coordinator: PeerMutationCoordinator = peer_mutations,
    clock: Callable[[], float] = time.time,
) -> list[StaleWireGuardPeer]:
    """Returns the validated Parker peers which have not had a recent handshake.

    Peers which never had a handshake are only returned after the initial handshake grace,
    malformed peers are skipped.
    """
    stale_before = clock() - stale_timeout
    logger.info(
        "Starting search for stale wireguard peers for interface %s.", wg_interface
    )
    stale_peers = []
    for peer_index, peer in enumerate(_all_peers(wg_interface)):
        try:
            public_key = _peer_public_key(peer)
            if coordinator.recently_provisioned(public_key, initial_handshake_grace):
                continue
            last_handshake = _last_handshake_seconds(peer)
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
            parker_prefix = _parker_prefix(peer, parker_prefix_length)
        except (UnicodeDecodeError, ValueError, AttributeError) as error:
            logger.warning(
                "Skipping malformed WireGuard peer interface=%s peer_index=%s reason=%s",
                wg_interface,
                peer_index,
                error,
            )
            continue
        stale_peers.append(
            StaleWireGuardPeer(
                public_key=public_key, parker_prefix=parker_prefix, reason=reason
            )
        )
    return stale_peers


def find_stale_wireguard_clients(
    parker: bool, wg_interface: str, timeout: int = _DEFAULT_PEER_TIMEOUT
) -> list[tuple[str, str | None]]:
    """Fetches and returns a list of peers which have not had recent handshakes.

    Arguments:
        wg_interface: The WireGuard interface to query.
        timeout: Seconds without handshake after which a peer is stale.

    Returns:
        A list of (public key, Parker range6 or None) of peers which have not recently seen a handshake.
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
        (
            peer.get_attr("WGPEER_A_PUBLIC_KEY").decode("utf-8"),
            (
                allowed_ips[0].get("addr")  # addr contains the prefix as string
                if parker
                and len(allowed_ips := peer.get_attr("WGPEER_A_ALLOWEDIPS") or []) > 0
                else None
            ),
        )
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
