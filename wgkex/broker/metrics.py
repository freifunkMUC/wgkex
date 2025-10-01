import dataclasses
from operator import itemgetter
from typing import Any, Dict, Optional, Tuple

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
        """Analyzes the metrics and determines the best worker that a new client should connect to.
        The best worker is defined as the one with the most number of clients missing
        to its should-be target value according to its weight.

        Returns:
            A 3-tuple containing the worker name, difference to target peers, number of connected peers.
            The worker name can be None if none is online.
        """
        # Map metrics to a list of (target diff, peer count, worker) tuples for online workers

        peers_worker_tuples = []
        total_peers = self.get_total_peer_count()
        worker_cfg = config.get_config().workers

        for wm in self.data.values():
            if not wm.is_online(domain):
                continue

            peers = wm.get_peer_count()
            rel_weight = worker_cfg.relative_worker_weight(wm.worker)
            target = rel_weight * total_peers
            diff = peers - target
            logger.debug(
                f"Worker candidate {wm.worker}: current {peers}, target {target} (total {total_peers}, rel weight {rel_weight}), diff {diff}"
            )
            peers_worker_tuples.append((diff, peers, wm.worker))

        # Sort by diff (ascending), workers with most peers missing to target are sorted first
        peers_worker_tuples = sorted(peers_worker_tuples, key=itemgetter(0))

        if len(peers_worker_tuples) > 0:
            best = peers_worker_tuples[0]
            return best[2], best[0], best[1]
        return None, 0, 0
