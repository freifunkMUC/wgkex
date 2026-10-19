"""Prometheus metrics of the broker, served at /metrics."""

from collections.abc import Iterator

from prometheus_client import CollectorRegistry, Counter
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from wgkex.broker.metrics import WorkerMetricsCollection

REGISTRY = CollectorRegistry(auto_describe=True)

KEY_EXCHANGE_REQUESTS = Counter(
    "wgkex_broker_key_exchange_requests",
    "Key exchange requests by API version and result",
    ["api", "result"],
    registry=REGISTRY,
)
ASSIGNMENTS = Counter(
    "wgkex_broker_assignments",
    "Peers assigned to a worker by the v2 and v3 (Parker) key exchange",
    ["worker", "domain"],
    registry=REGISTRY,
)
REBALANCE_HINTS = Counter(
    "wgkex_broker_rebalance_hints",
    "Rebalancing hints published to workers, kind is rebalance or drain",
    ["worker", "kind"],
    registry=REGISTRY,
)
REBALANCE_HINT_EXCESS = Counter(
    "wgkex_broker_rebalance_hint_excess_peers",
    "Sum of the excess peers in published rebalancing hints",
    ["worker", "kind"],
    registry=REGISTRY,
)
SHARED_ASSIGNMENTS = Counter(
    "wgkex_broker_shared_assignments_received",
    "Assignments received from other broker instances",
    registry=REGISTRY,
)
MQTT_MESSAGE_ERRORS = Counter(
    "wgkex_broker_mqtt_message_errors",
    "MQTT messages which could not be processed, by handler",
    ["handler"],
    registry=REGISTRY,
)


class WorkerCollector(Collector):
    """Exports the current worker state of the broker at scrape time."""

    def __init__(self, worker_metrics: WorkerMetricsCollection) -> None:
        self._worker_metrics = worker_metrics

    def collect(self) -> Iterator[GaugeMetricFamily]:
        online = GaugeMetricFamily(
            "wgkex_broker_worker_online",
            "Whether the worker is online",
            labels=["worker"],
        )
        draining = GaugeMetricFamily(
            "wgkex_broker_worker_draining",
            "Whether the worker is draining",
            labels=["worker"],
        )
        connected = GaugeMetricFamily(
            "wgkex_broker_worker_connected_peers",
            "Connected peers reported by the worker over all domains",
            labels=["worker"],
        )
        pending = GaugeMetricFamily(
            "wgkex_broker_worker_pending_assignments",
            "Assigned peers not yet reflected in the worker's metrics",
            labels=["worker"],
        )
        effective = GaugeMetricFamily(
            "wgkex_broker_worker_effective_peers",
            "Connected peers plus pending assignments",
            labels=["worker"],
        )
        target = GaugeMetricFamily(
            "wgkex_broker_worker_target_peers",
            "Weighted share of the effective peers of all active workers",
            labels=["worker"],
        )
        domain_online = GaugeMetricFamily(
            "wgkex_broker_worker_domain_online",
            "Whether the worker is online for the domain (with fresh metrics)",
            labels=["worker", "domain"],
        )
        domain_connected = GaugeMetricFamily(
            "wgkex_broker_worker_domain_connected_peers",
            "Connected peers reported by the worker for the domain",
            labels=["worker", "domain"],
        )
        for s in self._worker_metrics.snapshot():
            online.add_metric([s.worker], int(s.online))
            draining.add_metric([s.worker], int(s.draining))
            connected.add_metric([s.worker], s.connected_peers)
            pending.add_metric([s.worker], s.pending)
            effective.add_metric([s.worker], s.effective_peers)
            target.add_metric([s.worker], s.target)
            for domain, (d_online, d_peers) in s.domains.items():
                domain_online.add_metric([s.worker, domain], int(d_online))
                domain_connected.add_metric([s.worker, domain], d_peers)
        yield from (
            online,
            draining,
            connected,
            pending,
            effective,
            target,
            domain_online,
            domain_connected,
        )

    def describe(self) -> Iterator[GaugeMetricFamily]:
        # Avoids calling collect() (and taking the lock) on registration
        return iter(())


_collector: WorkerCollector | None = None


def register_worker_metrics(worker_metrics: WorkerMetricsCollection) -> None:
    """Exports the given worker metrics, replacing a previously registered collection."""
    global _collector
    if _collector is not None:
        REGISTRY.unregister(_collector)
    _collector = WorkerCollector(worker_metrics)
    REGISTRY.register(_collector)
