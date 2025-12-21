import dataclasses
from typing import Any, Dict, List, Optional, Tuple

from wgkex.common import logger
from wgkex.common.mqtt import MQTTTopics
from wgkex.config import config


@dataclasses.dataclass
class WorkerMetrics:
    """Metrics of a single worker"""

    worker: str
    #            domain -> [metric name -> metric data]
    domain_data: Dict[str, Dict[str, Any]] = dataclasses.field(default_factory=dict)
    online: bool = False

    def is_online(self, domain: str = "") -> bool:
        if domain:
            return (
                self.online
                and self.get_domain_metrics(domain).get(
                    MQTTTopics.CONNECTED_PEERS_METRIC, -1
                )
                >= 0
            )
        else:
            return self.online

    def get_domain_metrics(self, domain: str) -> Dict[str, Any]:
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
                data.get(MQTTTopics.CONNECTED_PEERS_METRIC, 0),
                0,
            )

        return total


@dataclasses.dataclass
class WorkerResult:
    name: str
    id: int
    diff: int
    peers: int
    target: int


@dataclasses.dataclass
class WorkerMetricsCollection:
    """A container for all worker metrics
    # TODO make threadsafe / fix data races
    """

    #     worker -> WorkerMetrics
    data: Dict[str, WorkerMetrics] = dataclasses.field(default_factory=dict)

    def get(self, worker: str) -> WorkerMetrics:
        return self.data.get(worker, WorkerMetrics(worker=worker))

    def set(self, worker: str, metrics: WorkerMetrics) -> None:
        self.data[worker] = metrics

    def update(self, worker: str, domain: str, metric: str, value: Any) -> None:
        if worker in self.data:
            self.data[worker].set_metric(domain, metric, value)
        else:
            metrics = WorkerMetrics(worker)
            metrics.set_metric(domain, metric, value)
            self.data[worker] = metrics

    def set_online(self, worker: str) -> None:
        if worker in self.data:
            self.data[worker].online = True
        else:
            metrics = WorkerMetrics(worker)
            metrics.online = True
            self.data[worker] = metrics

    def set_offline(self, worker: str) -> None:
        if worker in self.data:
            self.data[worker].online = False

    def get_total_peer_count(self) -> int:
        """Returns the sum of connected peers over all workers and domains"""
        total = 0
        for worker in self.data:
            worker_data = self.data.get(worker)
            if not worker_data:
                continue
            for domain in worker_data.domain_data:
                total += max(
                    worker_data.get_domain_metrics(domain).get(
                        MQTTTopics.CONNECTED_PEERS_METRIC, 0
                    ),
                    0,
                )

        return total

    def get_best_worker(self, domain: str) -> Tuple[Optional[str], int, int]:
        """See get_best_workers(), but only returns a single best worker.
        The worker name can be None if none is online.
        """
        workers = self.get_best_workers(domain, [])
        if len(workers) == 0:
            return None, 0, 0
        # Sort by diff (ascending), workers with most peers missing to target are sorted first
        worker = sorted(workers, key=lambda wr: wr.diff)[0]
        return (worker.name, worker.diff, worker.peers)

    def get_best_workers(
        self, domain: str, current_selected_workers: Optional[List[str]]
    ) -> List[WorkerResult]:
        """Analyzes the metrics and determines the best worker for each PoP that a node should connect to.

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

        new_selected_workers: List[WorkerResult] = []
        workers_cfg = config.get_config().workers

        print(workers_cfg.all_pops)
        print(self.data.values())

        for pop in workers_cfg.all_pops:
            candidates: List[WorkerResult] = []
            total_peers = self.get_total_peer_count()

            # Map metrics to a list of (target diff, peer count, worker) tuples for online workers
            for wm in self.data.values():
                worker_cfg = workers_cfg.get(wm.worker) or config.Worker(
                    id=0, weight=1, pop=""
                )

                print(worker_cfg)

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
