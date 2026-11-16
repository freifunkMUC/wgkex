"""Prometheus metrics of the worker, served on an optional HTTP listener."""

from wsgiref.simple_server import WSGIServer

import prometheus_client
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from wgkex.common import logger

REGISTRY = CollectorRegistry(auto_describe=True)

QUEUE_DEPTH = Gauge(
    "wgkex_worker_queue_depth",
    "Peer operations waiting in the queue",
    registry=REGISTRY,
)
PEER_OPS = Counter(
    "wgkex_worker_peer_operations",
    "Processed peer operations (op is add or remove, result is ok or error)",
    ["op", "result"],
    registry=REGISTRY,
)
NETLINK_OP_SECONDS = Histogram(
    "wgkex_worker_netlink_operation_seconds",
    "Duration of netlink operations, including a retry",
    ["op"],
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
    registry=REGISTRY,
)
NETLINK_RECONNECTS = Counter(
    "wgkex_worker_netlink_reconnects",
    "Netlink sockets reopened after an error",
    registry=REGISTRY,
)
EVICTIONS = Counter(
    "wgkex_worker_evictions",
    "Peers evicted on request of the broker (kind is rebalance or drain)",
    ["kind"],
    registry=REGISTRY,
)


def start_http_server(port: int | None, host: str) -> WSGIServer | None:
    """Starts the metrics listener in a daemon thread if a port is configured.

    Returns:
        The server, or None if no port is configured.
    """
    if not port:
        return None
    server, _ = prometheus_client.start_http_server(port, addr=host, registry=REGISTRY)
    logger.info("Serving Prometheus metrics on %s:%d", host, port)
    return server
