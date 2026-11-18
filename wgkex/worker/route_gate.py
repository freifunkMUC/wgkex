"""Handshake-gated Parker routes for multi-site active/standby.

A node has a peer on every pgw of a site, but sends traffic through one pgw
only. The gate installs the route to the prefix of a node only on the pgw
where the node has a fresh handshake. All other pgws keep the peer without
a route, so they do not attract the return traffic of the node.
"""

import ipaddress
import socket
import threading
from collections.abc import Callable
from time import time

import pyroute2

from wgkex.common import logger
from wgkex.worker.netlink import (
    _last_handshake_seconds,
    _netlink_call,
    _parker_prefix,
    _peer_public_key,
    _wireguard_info,
)
from wgkex.worker.peer_state import PeerMutationCoordinator, peer_mutations


class RouteGate:
    """Keeps the /63 routes on wg-nodes equal to the peers with a fresh handshake.

    Attributes:
        max_age: A handshake older than this (seconds) is not fresh.
        prefix_length: The length of the Parker prefix of a node.
        on_route: Called with the public key and the handshake time when the
            gate adds a route.
    """

    def __init__(
        self,
        max_age: float,
        prefix_length: int,
        on_route: Callable[[str, float], None],
        *,
        wg_interface: str = "wg-nodes",
        coordinator: PeerMutationCoordinator = peer_mutations,
        clock: Callable[[], float] = time,
    ) -> None:
        self.max_age = max_age
        self.prefix_length = prefix_length
        self.on_route = on_route
        self.wg_interface = wg_interface
        self.coordinator = coordinator
        self.clock = clock
        # Public key -> time of the unroute. The route comes back only after
        # a handshake that is newer than this time.
        self._held: dict[str, float] = {}
        self._lock = threading.Lock()

    def unroute(self, public_key: str, handshake: float | None = None) -> None:
        """Remove the route of a node and keep its peer.

        Arguments:
            public_key: The public key of the node.
            handshake: The handshake time of the node on its new pgw. A local
                handshake that is newer than this time keeps the route, so a
                late message does not remove the route after a quick move back.
                None: hold the route until the next handshake.
        """
        with self._lock:
            now = self.clock()
            self._held[public_key] = now if handshake is None else min(handshake, now)
        logger.info("Unroute requested public_key=%s", public_key)
        self.sync()

    def _fresh_prefixes(self) -> dict[str, tuple[str, float]]:
        """Return prefix -> (public key, handshake) for all peers with a fresh handshake."""
        now = self.clock()
        messages = _wireguard_info(self.wg_interface)
        fresh: dict[str, tuple[str, float]] = {}
        for message in messages:
            peers = message.get_attr("WGDEVICE_A_PEERS")
            if not isinstance(peers, (list, tuple)):
                continue
            for peer in peers:
                try:
                    public_key = _peer_public_key(peer)
                    handshake = _last_handshake_seconds(peer)
                    if not handshake or now - handshake > self.max_age:
                        continue
                    if handshake <= self._held.get(public_key, -1):
                        continue
                    fresh[_parker_prefix(peer, self.prefix_length)] = (
                        public_key,
                        handshake,
                    )
                except (UnicodeDecodeError, ValueError, AttributeError):
                    continue
        return fresh

    def _routed_prefixes(self, ip: pyroute2.IPRoute, oif: int) -> set[str]:
        routed = set()
        for route in ip.get_routes(family=socket.AF_INET6, oif=oif):
            dst = route.get_attr("RTA_DST")
            if dst and route["dst_len"] == self.prefix_length:
                routed.add(str(ipaddress.ip_network(f"{dst}/{route['dst_len']}")))
        return routed

    def sync(self) -> list[str]:
        """Add and remove routes once. Returns the public keys that got a route."""
        with self._lock:
            now = self.clock()
            self._held = {
                key: held_at
                for key, held_at in self._held.items()
                if now - held_at <= self.max_age
            }
            fresh = self._fresh_prefixes()
            routed_keys = []
            oif = _netlink_call(lambda h: h.link_index(self.wg_interface), "route_gate")
            routed = _netlink_call(
                lambda h: self._routed_prefixes(h.iproute(), oif), "route_gate"
            )
            for prefix in sorted(routed - fresh.keys()):
                self._route("del", prefix, oif)
            for prefix in sorted(fresh.keys() - routed):
                if self._route("replace", prefix, oif):
                    routed_keys.append(fresh[prefix])
        for public_key, handshake in routed_keys:
            self.on_route(public_key, handshake)
        return [public_key for public_key, _ in routed_keys]

    def _route(self, action: str, prefix: str, oif: int) -> bool:
        with self.coordinator.peer_lock("", prefix):
            try:
                _netlink_call(
                    lambda h: h.iproute().route(action, dst=prefix, oif=oif),
                    "route_gate",
                )
            except pyroute2.netlink.exceptions.NetlinkError as error:
                logger.warning(
                    "Route gate %s failed prefix=%s error=%s", action, prefix, error
                )
                return False
        logger.info("Route gate %s prefix=%s", action, prefix)
        return True

    def run(self, exit_event: threading.Event, interval: float) -> None:
        while not exit_event.is_set():
            try:
                self.sync()
            except Exception as error:  # noqa: BLE001
                logger.error("Route gate sync failed: %s", error, exc_info=error)
            exit_event.wait(interval)


gate: RouteGate | None = None
