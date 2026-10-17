"""Active rebalancing: evicts a few connected peers when the broker reports this worker as overloaded.

Gluon nodes (ffmuc-mesh-vpn-wireguard-vxlan) check their uplink every minute and redo the key
exchange when the tunnel is broken. Removing the WireGuard peer on an overloaded worker therefore
makes the node reconnect within ~1-2 minutes, and the broker assigns it to an underloaded worker.

Peer choice: we evict the peers whose key was most recently (re-)added on this worker first. These
are typically nodes which just moved here because another gateway was down, and are the ones whose
connection is youngest. Peers of unknown age (e.g. after a worker restart) come last, in random order.
A peer is never evicted twice within the configured cooldown to avoid ping-pong.
"""

import dataclasses
import json
import random
import threading
import time
from collections.abc import Callable

from wgkex.common import logger
from wgkex.config.config import Rebalancing
from wgkex.worker.allowlist import is_allowlisted
from wgkex.worker.netlink import WireGuardClient, get_connected_peers, link_handler
from wgkex.worker.observability import EVICTIONS


class PeerAddTracker:
    """Thread-safe record of when peers were last added by the key exchange on this worker."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        #          (wg interface, public key) -> time
        self._added_at: dict[tuple[str, str], float] = {}

    def note_added(self, wg_interface: str, public_key: str) -> None:
        with self._lock:
            self._added_at[(wg_interface, public_key)] = self._clock()

    def added_at(self, wg_interface: str, public_key: str) -> float | None:
        with self._lock:
            return self._added_at.get((wg_interface, public_key))

    def now(self) -> float:
        return self._clock()

    def forget(self, wg_interface: str, public_key: str) -> None:
        with self._lock:
            self._added_at.pop((wg_interface, public_key), None)


peer_add_tracker = PeerAddTracker()


@dataclasses.dataclass
class _Instruction:
    excess: int
    domains: list[str]
    received_at: float
    # Overrides the configured max_evictions_ratio, sent by the broker while this worker is draining
    max_evictions_ratio: float | None = None


class Rebalancer:
    """Evicts peers on request of the broker, rate-limited.

    Arguments:
        cfg: The rebalancing configuration.
        domains: The domains served by this worker.
        wg_interface_name: Function mapping a domain to its WireGuard interface name.
        tracker: Record of peer add times.
        evict: Function evicting a peer, defaults to removing it via netlink.
        connected_peers: Function returning connected peers for a WireGuard interface.
        protected: Function returning True for public keys which must never be evicted
            (allowlisted keys).
    """

    def __init__(
        self,
        cfg: Rebalancing,
        domains: list[str],
        wg_interface_name: Callable[[str], str | None],
        tracker: PeerAddTracker = peer_add_tracker,
        evict: Callable[[str, str], object] | None = None,
        connected_peers: Callable[[str], list[str]] = get_connected_peers,
        protected: Callable[[str], bool] = is_allowlisted,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self._cfg = cfg
        self._domains = domains
        self._wg_interface_name = wg_interface_name
        self._tracker = tracker
        self._evict = evict or _evict_peer
        self._connected_peers = connected_peers
        self._protected = protected
        self._clock = clock
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._instruction: _Instruction | None = None
        self._evicted_at: dict[str, float] = {}

    def handle_instruction(self, payload: bytes | str) -> None:
        """Parses and stores a rebalancing hint from the broker. Invalid hints are ignored."""
        try:
            data = json.loads(payload)
            excess = int(data["excess"])
            domains = [d for d in data["domains"] if d in self._domains]
            ratio = data.get("max_evictions_ratio")
            if ratio is not None:
                ratio = min(max(float(ratio), 0.0), 1.0)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            logger.error("Ignoring invalid rebalancing hint %r: %s", payload, e)
            return
        with self._lock:
            self._instruction = _Instruction(
                excess=excess,
                domains=domains,
                received_at=self._clock(),
                max_evictions_ratio=ratio,
            )
        logger.info(
            "Received rebalancing hint: %d excess peers, domains %s%s",
            excess,
            domains,
            f", drain rate {ratio}" if ratio is not None else "",
        )

    def run_once(self) -> list[str]:
        """Evicts up to the allowed number of peers for the current hint.

        Returns:
            The public keys of the evicted peers.
        """
        now = self._clock()
        with self._lock:
            instruction = self._instruction
            # Only act on fresh hints, the broker stops sending them once we are within tolerance
            if (
                instruction is None
                or instruction.excess <= 0
                or now - instruction.received_at > 2.5 * self._cfg.interval
            ):
                return []
            self._evicted_at = {
                k: t
                for k, t in self._evicted_at.items()
                if now - t < self._cfg.eviction_cooldown
            }
            evicted_recently = set(self._evicted_at)

        total_connected = 0
        candidates: list[tuple[str, str]] = []
        for domain in self._domains:
            iface = self._wg_interface_name(domain)
            if not iface:
                continue
            peers = self._connected_peers(iface)
            total_connected += len(peers)
            if domain in instruction.domains:
                candidates.extend(
                    (iface, key)
                    for key in peers
                    if key not in evicted_recently and not self._protected(key)
                )

        ratio = (
            instruction.max_evictions_ratio
            if instruction.max_evictions_ratio is not None
            else self._cfg.max_evictions_ratio
        )
        kind = "rebalance" if instruction.max_evictions_ratio is None else "drain"
        budget = min(
            instruction.excess,
            max(1, int(total_connected * ratio)),
            len(candidates),
        )
        if budget <= 0:
            return []

        # Random tie-break, then most recently added first (unknown age last)
        self._rng.shuffle(candidates)
        candidates.sort(
            key=lambda c: self._tracker.added_at(*c) or float("-inf"), reverse=True
        )

        evicted = []
        for iface, key in candidates[:budget]:
            logger.info("Rebalancing: evicting peer %s from %s", key, iface)
            try:
                self._evict(iface, key)
            except Exception as e:  # noqa: BLE001
                logger.error("Failed to evict peer %s from %s: %s", key, iface, e)
                continue
            self._tracker.forget(iface, key)
            evicted.append(key)
            EVICTIONS.labels(kind).inc()

        with self._lock:
            for key in evicted:
                self._evicted_at[key] = now
            if self._instruction is instruction:
                instruction.excess -= len(evicted)
        return evicted

    def loop(self, exit_event: threading.Event) -> None:
        """Runs a rebalancing round every interval until exit_event is set."""
        while not exit_event.wait(self._cfg.interval):
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001
                # Don't crash the thread when an exception is encountered
                logger.error("Exception during rebalancing task: %s", e, exc_info=True)


def _evict_peer(wg_interface: str, public_key: str) -> dict:
    """Removes a peer including its route and FDB entry, like the stale peer cleanup does."""
    return link_handler(
        WireGuardClient(
            public_key=public_key,
            domain=wg_interface.removeprefix("wg-"),
            remove=True,
        )
    )
