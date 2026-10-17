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

    def is_online(self, domain: str = "") -> bool:
        if domain:
            return (
                self.online
                and self.get_domain_metrics(domain).get(CONNECTED_PEERS_METRIC, -1) >= 0
            )
        else:
            return self.online

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

    def to_dict(self) -> dict[str, Any]:
        return {"excess": self.excess, "domains": self.domains}


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
    clock: Callable[[], float] = dataclasses.field(default=time.monotonic, repr=False)
    rng: random.Random = dataclasses.field(default_factory=random.Random, repr=False)
    #        domain -> public key -> assignment
    _pending: dict[str, dict[str, PendingAssignment]] = dataclasses.field(
        default_factory=dict, repr=False
    )
    _lock: threading.RLock = dataclasses.field(
        default_factory=threading.RLock, repr=False, compare=False
    )
    # Time of the last change of the set of online workers (or broker start)
    _last_topology_change: float | None = dataclasses.field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self._last_topology_change is None:
            self._last_topology_change = self.clock()

    def get(self, worker: str) -> WorkerMetrics:
        with self._lock:
            return self.data.get(worker, WorkerMetrics(worker=worker))

    def set(self, worker: str, metrics: WorkerMetrics) -> None:
        with self._lock:
            self.data[worker] = metrics
            self._last_topology_change = self.clock()

    def update(self, worker: str, domain: str, metric: str, value: Any) -> None:
        with self._lock:
            if worker not in self.data:
                self.data[worker] = WorkerMetrics(worker)
            wm = self.data[worker]
            was_online = wm.is_online(domain)
            wm.set_metric(domain, metric, value)
            if was_online != wm.is_online(domain):
                self._last_topology_change = self.clock()

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
            if not self.data[worker].online:
                self.data[worker].online = True
                self._last_topology_change = self.clock()

    def set_offline(self, worker: str) -> None:
        with self._lock:
            if worker in self.data and self.data[worker].online:
                self.data[worker].online = False
                self._last_topology_change = self.clock()
                self._prune_pending(lambda p: p.worker == worker)

    def record_assignment(self, worker: str, domain: str, public_key: str) -> None:
        """Remembers that a peer was assigned to a worker, replacing an older assignment of the same peer."""
        with self._lock:
            self._pending.setdefault(domain, {})[public_key] = PendingAssignment(
                worker=worker, assigned_at=self.clock()
            )

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
        self, domain: str, eligible: Callable[[str], bool] | None = None
    ) -> tuple[str | None, float, int]:
        """Determines the worker that a new client should connect to.

        Only workers which are online for the domain (and accepted by `eligible`) are considered.
        Load is measured as connected peers over all domains plus pending assignments.
        The worker is chosen randomly with a probability proportional to the number of peers it is
        missing to its weighted target, so that concurrent requests and multiple broker instances
        spread clients instead of all choosing the same worker. If no worker is below its target,
        the worker is chosen randomly according to its weight.

        Returns:
            A 3-tuple containing the worker name, difference to target peers, number of effective peers.
            The worker name is None if no worker is online.
        """
        with self._lock:
            candidates = [
                wm.worker
                for wm in self.data.values()
                if wm.is_online(domain) and (eligible is None or eligible(wm.worker))
            ]
            if not candidates:
                return None, 0, 0

            targets = self._targets(candidates)
            for w, (load, target) in targets.items():
                logger.debug(
                    f"Worker candidate {w}: effective peers {load}, target {target:.1f}, diff {load - target:.1f}"
                )

            deficits = [max(target - load, 0.0) for load, target in targets.values()]
            if sum(deficits) > 0:
                chances = deficits
            else:
                worker_cfg = config.get_config().workers
                chances = [worker_cfg.relative_worker_weight(w) for w in candidates]
            chosen = self.rng.choices(candidates, weights=chances, k=1)[0]
            load, target = targets[chosen]
            return chosen, load - target, load

    def is_topology_stable(self, stable_time: float) -> bool:
        """Whether no worker went online or offline (and the broker did not start) within stable_time seconds."""
        with self._lock:
            return self.clock() - self._last_topology_change >= stable_time

    def compute_rebalance_hints(
        self,
        domains: list[str],
        tolerance: float,
        min_excess: int,
        stable_time: float,
    ) -> dict[str, RebalanceHint]:
        """Computes which workers should shed peers to approach their weighted target.

        Hints are only produced if the set of online workers has been stable for stable_time seconds,
        for workers whose excess exceeds max(min_excess, tolerance * target), and only for domains in
        which another worker below its target is online, so that evicted peers have somewhere to go.

        Returns:
            A dict mapping worker names to rebalance hints.
        """
        with self._lock:
            if not self.is_topology_stable(stable_time):
                return {}

            online = [wm.worker for wm in self.data.values() if wm.is_online()]
            if len(online) < 2:
                return {}
            targets = self._targets(online)
            underloaded = {w for w, (load, target) in targets.items() if load < target}

            hints = {}
            for w, (load, target) in targets.items():
                excess = load - target
                if excess <= max(min_excess, tolerance * target):
                    continue
                hint_domains = [
                    d
                    for d in domains
                    if self.data[w].is_online(d)
                    and any(self.data[u].is_online(d) for u in underloaded)
                ]
                if hint_domains:
                    hints[w] = RebalanceHint(excess=int(excess), domains=hint_domains)
            return hints
