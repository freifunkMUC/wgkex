import dataclasses
import random
import threading
import time
from collections.abc import Callable
from typing import Any

from wgkex.common import logger
from wgkex.common.mqtt import CONNECTED_PEERS_METRIC
from wgkex.config import config


@dataclasses.dataclass
class WorkerMetrics:
    """Metrics of a single worker"""

    worker: str
    #            domain -> [metric name -> metric data]
    domain_data: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    online: bool = False
    # Draining workers (status 2) get no new peers and shed their existing ones
    draining: bool = False
    #            domain -> time the connected peers metric was last received
    updated_at: dict[str, float] = dataclasses.field(default_factory=dict)

    def is_online(
        self, domain: str = "", now: float | None = None, max_age: float = 0
    ) -> bool:
        """Whether the worker is online, optionally for a specific domain.

        A worker is online for a domain if it has a non-negative connected peers metric for it,
        which (if now and max_age are given) has been received within max_age seconds.
        """
        if not domain:
            return self.online
        if not (
            self.online
            and self.get_domain_metrics(domain).get(CONNECTED_PEERS_METRIC, -1) >= 0
        ):
            return False
        if max_age > 0 and now is not None and domain in self.updated_at:
            return now - self.updated_at[domain] <= max_age
        return True

    def get_domain_metrics(self, domain: str) -> dict[str, Any]:
        return self.domain_data.get(domain, {})

    def set_metric(self, domain: str, metric: str, value: Any) -> None:
        if domain in self.domain_data:
            self.domain_data[domain][metric] = value
        else:
            self.domain_data[domain] = {metric: value}

    def get_peer_count(self) -> int:
        """Returns the sum of connected peers on this worker over all domains"""
        total = 0
        for data in self.domain_data.values():
            total += max(
                data.get(CONNECTED_PEERS_METRIC, 0),
                0,
            )

        return total


@dataclasses.dataclass
class PendingAssignment:
    """A peer which was assigned to a worker but might not be reflected in its metrics yet."""

    worker: str
    assigned_at: float


@dataclasses.dataclass
class RebalanceHint:
    """Instruction for an overloaded worker to shed peers.

    Attributes:
        excess: Number of peers the worker has above its weighted target.
        domains: Domains in which peers may be evicted, i.e. where an underloaded worker is online.
    """

    excess: int
    domains: list[str]
    # Only set for draining workers, overrides the eviction rate configured on the worker
    max_evictions_ratio: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"excess": self.excess, "domains": self.domains}
        if self.max_evictions_ratio is not None:
            d["max_evictions_ratio"] = self.max_evictions_ratio
        return d


@dataclasses.dataclass
class WorkerSnapshot:
    """State of a worker at one point in time, see WorkerMetricsCollection.snapshot."""

    worker: str
    online: bool
    draining: bool
    connected_peers: int
    pending: int
    effective_peers: int
    target: float
    #            domain -> (online, connected peers)
    domains: dict[str, tuple[bool, int]]


@dataclasses.dataclass
class WorkerResult:
    name: str
    id: int
    diff: int
    peers: int
    target: int


@dataclasses.dataclass
class WorkerMetricsCollection:
    """A thread-safe container for all worker metrics.

    Besides the metrics reported by the workers, it keeps track of peers which were recently
    assigned to a worker by the broker (pending assignments). The connected peers metric of a worker
    only reflects a new peer after its first handshake and the next metrics publication, so without
    them a burst of key exchanges (e.g. after a gateway went down) would all be counted against the
    same stale metric and be sent to the same worker.

    Attributes:
        pending_ttl: Seconds after which a pending assignment is dropped in any case.
        pending_settle_time: Seconds after which a pending assignment is assumed to be reflected
            in a newly received connected peers metric of its worker.
        clock: Monotonic time source, overridable for tests.
        rng: Random number generator used for worker selection, overridable for tests.
    """

    #     worker -> WorkerMetrics
    data: dict[str, WorkerMetrics] = dataclasses.field(default_factory=dict)
    pending_ttl: float = 300
    pending_settle_time: float = 60
    metrics_max_age: float = 0
    clock: Callable[[], float] = dataclasses.field(default=time.monotonic, repr=False)
    rng: random.Random = dataclasses.field(default_factory=random.Random, repr=False)
    #        domain -> public key -> assignment
    _pending: dict[str, dict[str, PendingAssignment]] = dataclasses.field(
        default_factory=dict, repr=False
    )
    _lock: threading.RLock = dataclasses.field(
        default_factory=threading.RLock, repr=False, compare=False
    )
    # Time of the last change of the set of online workers/domains (or broker start)
    _last_topology_change: float | None = dataclasses.field(default=None, repr=False)
    _online_snapshot: frozenset[tuple[str, str]] = dataclasses.field(
        default_factory=frozenset, repr=False
    )

    def __post_init__(self) -> None:
        if self._last_topology_change is None:
            self._last_topology_change = self.clock()

    def _is_online(self, wm: WorkerMetrics, domain: str = "") -> bool:
        return wm.is_online(domain, self.clock(), self.metrics_max_age)

    def _refresh_topology(self) -> None:
        """Records the time of a change in the set of online workers and domains.

        Called on every update and when the topology is queried, so that workers whose metrics
        became stale are noticed as well.
        """
        snapshot = frozenset(
            (wm.worker, domain)
            for wm in self.data.values()
            if wm.online
            for domain in [""] + list(wm.domain_data)
            if self._is_online(wm, domain)
        )
        if snapshot != self._online_snapshot:
            self._online_snapshot = snapshot
            self._last_topology_change = self.clock()

    def get(self, worker: str) -> WorkerMetrics:
        with self._lock:
            return self.data.get(worker, WorkerMetrics(worker=worker))

    def is_online(self, worker: str, domain: str = "") -> bool:
        """Whether a worker is online (for a domain, taking the metrics age into account)."""
        with self._lock:
            wm = self.data.get(worker)
            return wm is not None and self._is_online(wm, domain)

    def set(self, worker: str, metrics: WorkerMetrics) -> None:
        with self._lock:
            self.data[worker] = metrics
            self._refresh_topology()

    def update(self, worker: str, domain: str, metric: str, value: Any) -> None:
        with self._lock:
            if worker not in self.data:
                self.data[worker] = WorkerMetrics(worker)
            wm = self.data[worker]
            wm.set_metric(domain, metric, value)
            if metric == CONNECTED_PEERS_METRIC:
                wm.updated_at[domain] = self.clock()
            self._refresh_topology()

            if (
                metric == CONNECTED_PEERS_METRIC
                and isinstance(value, int)
                and value >= 0
            ):
                # Assignments old enough to be contained in this metric are no longer pending
                self._prune_pending(
                    lambda p: p.worker == worker
                    and p.assigned_at <= self.clock() - self.pending_settle_time,
                    domain=domain,
                )

    def set_online(self, worker: str) -> None:
        with self._lock:
            if worker not in self.data:
                self.data[worker] = WorkerMetrics(worker)
            self.data[worker].online = True
            self._refresh_topology()

    def set_draining(self, worker: str, draining: bool) -> None:
        with self._lock:
            if worker not in self.data:
                self.data[worker] = WorkerMetrics(worker)
            self.data[worker].draining = draining

    def set_offline(self, worker: str) -> None:
        with self._lock:
            if worker in self.data and self.data[worker].online:
                self.data[worker].online = False
                self.data[worker].draining = False
                self._refresh_topology()
                self._prune_pending(lambda p: p.worker == worker)

    def record_assignment(self, worker: str, domain: str, public_key: str) -> None:
        """Remembers that a peer was assigned to a worker, replacing an older assignment of the same peer."""
        with self._lock:
            self._pending.setdefault(domain, {})[public_key] = PendingAssignment(
                worker=worker, assigned_at=self.clock()
            )

    def get_pending_worker(self, domain: str, public_key: str) -> str | None:
        """Returns the worker a key is pending on in a domain, if any."""
        with self._lock:
            self._prune_expired()
            p = self._pending.get(domain, {}).get(public_key)
            return p.worker if p is not None else None

    def get_pending_count(self, worker: str) -> int:
        """Returns the number of pending assignments of a worker over all domains."""
        with self._lock:
            self._prune_expired()
            return sum(
                1
                for assignments in self._pending.values()
                for p in assignments.values()
                if p.worker == worker
            )

    def get_effective_peer_count(self, worker: str) -> int:
        """Returns the reported connected peers plus pending assignments of a worker."""
        with self._lock:
            return self.get(worker).get_peer_count() + self.get_pending_count(worker)

    def get_total_peer_count(self) -> int:
        """Returns the sum of connected peers over all workers and domains"""
        with self._lock:
            return sum(wm.get_peer_count() for wm in self.data.values())

    def _prune_expired(self) -> None:
        deadline = self.clock() - self.pending_ttl
        self._prune_pending(lambda p: p.assigned_at <= deadline)

    def _prune_pending(
        self, predicate: Callable[[PendingAssignment], bool], domain: str | None = None
    ) -> None:
        domains = [domain] if domain is not None else list(self._pending)
        for d in domains:
            assignments = self._pending.get(d)
            if not assignments:
                continue
            for key in [k for k, p in assignments.items() if predicate(p)]:
                del assignments[key]
            if not assignments:
                del self._pending[d]

    def _targets(self, workers: list[str]) -> dict[str, tuple[int, float]]:
        """Returns worker -> (effective peers, weighted target) for the given set of workers.

        The target of a worker is its share of the effective peers of all given workers,
        according to the configured worker weights.
        """
        worker_cfg = config.get_config().workers
        loads = {w: self.get_effective_peer_count(w) for w in workers}
        weights = {w: worker_cfg.relative_worker_weight(w) for w in workers}
        total_load = sum(loads.values())
        total_weight = sum(weights.values()) or 1
        return {w: (loads[w], weights[w] / total_weight * total_load) for w in workers}

    def get_best_worker(
        self,
        domain: str,
        eligible: Callable[[str], bool] | None = None,
        location: str | None = None,
        location_tolerance: float = 0.1,
        location_min_excess: int = 10,
    ) -> tuple[str | None, float, int]:
        """Determines the worker that a new client should connect to.

        Only workers which are online for the domain (and accepted by `eligible`) are considered.
        Draining workers are only used if all of them are draining, so that a client always gets an
        endpoint while any worker is online.
        Load is measured as connected peers over all domains plus pending assignments.
        The worker is chosen randomly with a probability proportional to the number of peers it is
        missing to its weighted target, so that concurrent requests and multiple broker instances
        spread clients instead of all choosing the same worker. If no worker is below its target,
        the worker is chosen randomly according to its weight.

        If a location is given, workers configured with that location are preferred as long as
        their effective peers (including the new one) stay within
        target + max(location_min_excess, location_tolerance * target). Among those, the worker is
        chosen randomly proportional to the remaining room. Otherwise the normal selection applies.

        Returns:
            A 3-tuple containing the worker name, difference to target peers, number of effective peers.
            The worker name is None if no worker is online.
        """
        with self._lock:
            candidates = [
                wm.worker
                for wm in self.data.values()
                if self._is_online(wm, domain)
                and (eligible is None or eligible(wm.worker))
            ]
            if not candidates:
                return None, 0, 0
            not_draining = [w for w in candidates if not self.data[w].draining]
            if not_draining:
                candidates = not_draining

            targets = self._targets(candidates)
            for w, (load, target) in targets.items():
                logger.debug(
                    f"Worker candidate {w}: effective peers {load}, target {target:.1f}, diff {load - target:.1f}"
                )

            if location:
                local = self._pick_local(
                    candidates,
                    targets,
                    location,
                    location_tolerance,
                    location_min_excess,
                )
                if local is not None:
                    load, target = targets[local]
                    return local, load - target, load

            deficits = [max(target - load, 0.0) for load, target in targets.values()]
            if sum(deficits) > 0:
                chances = deficits
            else:
                worker_cfg = config.get_config().workers
                chances = [worker_cfg.relative_worker_weight(w) for w in candidates]
            chosen = self.rng.choices(candidates, weights=chances, k=1)[0]
            load, target = targets[chosen]
            return chosen, load - target, load

    def _pick_local(
        self,
        candidates: list[str],
        targets: dict[str, tuple[int, float]],
        location: str,
        tolerance: float,
        min_excess: int,
    ) -> str | None:
        worker_cfg = config.get_config().workers
        room = {}
        for w in candidates:
            if worker_cfg.location(w) != location:
                continue
            load, target = targets[w]
            allowed = target + max(min_excess, tolerance * target)
            if load + 1 <= allowed:
                room[w] = allowed - load
        if not room:
            return None
        return self.rng.choices(list(room), weights=list(room.values()), k=1)[0]

    def get_best_workers(
        self,
        domain: str,
        current_selected_workers: list[str] | None,
        require_configured: bool = False,
    ) -> list[WorkerResult]:
        """Analyzes the metrics and determines the best worker for each PoP that a node should connect to.

        With require_configured, workers that are missing from the workers
        config are skipped instead of getting a synthetic default config.
        Parker mode requires this: the worker's id is the concentrator ID
        identifying the tunnel on the node, so it must be unique and stable,
        which a synthetic default (id=0 for every unconfigured worker) is not.

        If no current_selected_workers is passed (None or empty):
            The best worker is defined as the one with the most number of clients missing
            to its should-be target value according to its weight.

        If current_selected_workers is passed:
            First it is checked whether the workers in the list are still online and not
            more than the configured treshold above the should-be target value.
            For any where this isn't the case, they are replaced as per the logic below:

            The best worker is defined as the one with the most number of clients missing
            to its should-be target value according to its weight.

        Returns:
            A List of WorkerResult containing the worker name, difference to target peers, number of connected peers.
            The list can be empty if no suitable worker could be determined.
        """

        new_selected_workers: list[WorkerResult] = []
        workers_cfg = config.get_config().workers

        for pop in workers_cfg.all_pops:
            candidates: list[WorkerResult] = []
            total_peers = self.get_total_peer_count()

            # Map metrics to a list of (target diff, peer count, worker) tuples for online workers
            for wm in self.data.values():
                worker_cfg = workers_cfg.get(wm.worker)
                if worker_cfg is None:
                    if require_configured:
                        logger.warning(
                            f"Worker {wm.worker} is not in the workers config, skipping"
                        )
                        continue
                    worker_cfg = config.Worker(id=0, weight=1, pop="")

                if worker_cfg.pop != pop:
                    continue

                if not wm.is_online(domain):
                    continue

                peers = wm.get_peer_count()
                rel_weight = workers_cfg.relative_worker_weight(wm.worker)
                target = round(rel_weight * total_peers)
                diff = peers - target
                logger.debug(
                    f"Worker candidate {wm.worker} for PoP {pop}: current {peers}, target {target} (total {total_peers}, rel weight {rel_weight}), diff {diff}"
                )
                candidates.append(
                    WorkerResult(wm.worker, worker_cfg.id, diff, peers, target)
                )

            # If one of the currently selected workers is a valid candidate, and below a certain treshold, keep it
            if current_selected_workers:
                any_hit = False

                for w in current_selected_workers:
                    all_matched = [cand for cand in candidates if cand.name == w]
                    if len(all_matched) > 0:
                        matched = all_matched[0]

                        if (
                            matched.diff > 0
                            and matched.diff
                            > workers_cfg.sticky_worker_tolerance * matched.target
                        ):
                            continue

                        new_selected_workers.append(matched)
                        any_hit = True
                        logger.debug(
                            f"Sticky worker candidate {matched.name} selected, below treshold"
                        )
                        break

                if any_hit:
                    continue  # to next PoP

            # Sort by diff (ascending), workers with most peers missing to target are sorted first
            candidates = sorted(candidates, key=lambda wm: wm.diff)

            if len(candidates) > 0:
                best = candidates[0]
                new_selected_workers.append(best)

        return new_selected_workers

    def is_topology_stable(self, stable_time: float) -> bool:
        """Whether no worker went online or offline (and the broker did not start) within stable_time seconds."""
        with self._lock:
            self._refresh_topology()
            return self.clock() - self._last_topology_change >= stable_time

    def compute_rebalance_hints(
        self,
        domains: list[str],
        tolerance: float,
        min_excess: int,
        stable_time: float,
        drain_rate: float | None = None,
    ) -> dict[str, RebalanceHint]:
        """Computes which workers should shed peers to approach their weighted target.

        Hints are only produced if the set of online workers has been stable for stable_time seconds,
        for workers whose excess exceeds max(min_excess, tolerance * target), and only for domains in
        which another worker below its target is online, so that evicted peers have somewhere to go.

        If drain_rate is given, draining workers are asked to shed all their peers at that rate,
        regardless of stability, in domains where a worker which is not draining is online.

        Returns:
            A dict mapping worker names to rebalance hints.
        """
        with self._lock:
            online = [
                wm.worker
                for wm in self.data.values()
                if self._is_online(wm)
                and any(self._is_online(wm, d) for d in wm.domain_data)
            ]
            active = [w for w in online if not self.data[w].draining]
            hints = {}
            if drain_rate is not None:
                hints.update(self._drain_hints(domains, online, active, drain_rate))

            if len(active) < 2 or not self.is_topology_stable(stable_time):
                return hints
            targets = self._targets(active)
            underloaded = {w for w, (load, target) in targets.items() if load < target}

            for w, (load, target) in targets.items():
                excess = load - target
                if excess <= max(min_excess, tolerance * target):
                    continue
                hint_domains = [
                    d
                    for d in domains
                    if self._is_online(self.data[w], d)
                    and any(self._is_online(self.data[u], d) for u in underloaded)
                ]
                if hint_domains:
                    hints[w] = RebalanceHint(excess=int(excess), domains=hint_domains)
            return hints

    def _drain_hints(
        self, domains: list[str], online: list[str], active: list[str], rate: float
    ) -> dict[str, RebalanceHint]:
        hints = {}
        for w in online:
            wm = self.data[w]
            if not wm.draining:
                continue
            hint_domains = [
                d
                for d in domains
                if self._is_online(wm, d)
                and any(self._is_online(self.data[a], d) for a in active)
            ]
            peers = sum(
                max(wm.get_domain_metrics(d).get(CONNECTED_PEERS_METRIC, 0), 0)
                for d in hint_domains
            )
            if hint_domains and peers > 0:
                hints[w] = RebalanceHint(
                    excess=peers, domains=hint_domains, max_evictions_ratio=rate
                )
        return hints

    def snapshot(self) -> list["WorkerSnapshot"]:
        """Returns a consistent view of all known workers, e.g. for exporting metrics.

        The target is the worker's weighted share of the effective peers of all online workers
        which are not draining, like in rebalancing; draining and offline workers have target 0.
        """
        with self._lock:
            online = {w for w, wm in self.data.items() if self._is_online(wm)}
            active = [w for w in sorted(online) if not self.data[w].draining]
            targets = self._targets(active)
            result = []
            for w in sorted(self.data):
                wm = self.data[w]
                result.append(
                    WorkerSnapshot(
                        worker=w,
                        online=w in online,
                        draining=wm.draining,
                        connected_peers=wm.get_peer_count(),
                        pending=self.get_pending_count(w),
                        effective_peers=self.get_effective_peer_count(w),
                        target=targets[w][1] if w in targets else 0.0,
                        domains={
                            d: (
                                self._is_online(wm, d),
                                max(
                                    wm.get_domain_metrics(d).get(
                                        CONNECTED_PEERS_METRIC, 0
                                    ),
                                    0,
                                ),
                            )
                            for d in sorted(wm.domain_data)
                        },
                    )
                )
            return result

    def is_draining(self, worker: str) -> bool:
        with self._lock:
            return worker in self.data and self.data[worker].draining
