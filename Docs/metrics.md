# Metrics and dashboards

Broker and worker export metrics in the Prometheus text format. Configuration is in the `metrics` section of
[`wgkex.yaml.example`](../wgkex.yaml.example).

## Broker metrics

The broker serves metrics at `GET /metrics` on its normal HTTP listener (`broker_listen`). Disable it with
`metrics.broker_endpoint: false`, or keep the path from being reachable through your reverse proxy if the worker loads
should not be public.

| Metric | Labels | Description |
|---|---|---|
| `wgkex_broker_worker_online` | `worker` | 1 if the worker is online |
| `wgkex_broker_worker_draining` | `worker` | 1 if the worker is draining |
| `wgkex_broker_worker_connected_peers` | `worker` | Connected peers reported by the worker (all domains) |
| `wgkex_broker_worker_pending_assignments` | `worker` | Assigned peers not yet reflected in the worker's metrics |
| `wgkex_broker_worker_effective_peers` | `worker` | Connected peers plus pending assignments, used for selection |
| `wgkex_broker_worker_target_peers` | `worker` | Weighted share of the effective peers of all online, non-draining workers |
| `wgkex_broker_worker_domain_online` | `worker`, `domain` | 1 if the worker is online for the domain with fresh metrics |
| `wgkex_broker_worker_domain_connected_peers` | `worker`, `domain` | Connected peers reported for the domain |
| `wgkex_broker_key_exchange_requests_total` | `api` (`v1`, `v2`), `result` (`ok`, `invalid`, `blocked`, `rate_limited`, `no_worker`, `no_worker_data`) | Key exchange requests |
| `wgkex_broker_assignments_total` | `worker`, `domain` | Peers assigned by the v2 key exchange |
| `wgkex_broker_rebalance_hints_total` | `worker`, `kind` (`rebalance`, `drain`) | Published rebalancing hints |
| `wgkex_broker_rebalance_hint_excess_peers_total` | `worker`, `kind` | Sum of the excess peers in published hints |
| `wgkex_broker_shared_assignments_received_total` | | Assignments received from other broker instances |
| `wgkex_broker_mqtt_message_errors_total` | `handler` (`metrics`, `status`, `data`, `assignment`) | Ignored invalid MQTT messages |

## Worker metrics

Workers serve metrics on a separate HTTP listener (`GET /metrics`), which is disabled unless `metrics.worker_port` is
set (e.g. `9101`; bind address `metrics.worker_host`, default `0.0.0.0`).

| Metric | Labels | Description |
|---|---|---|
| `wgkex_worker_queue_depth` | | Peer operations waiting in the queue |
| `wgkex_worker_peer_operations_total` | `op` (`add`, `remove`), `result` (`ok`, `error`) | Processed peer operations |
| `wgkex_worker_netlink_operation_seconds` | `op` (`wg_set`, `wg_info`, `route`, `fdb`, `get_addr`) | Histogram of netlink operation durations |
| `wgkex_worker_netlink_reconnects_total` | | Netlink sockets reopened after an error |
| `wgkex_worker_evictions_total` | `kind` (`rebalance`, `drain`) | Peers evicted on request of the broker |

## Grafana dashboards

[`contrib/grafana`](../contrib/grafana) contains example dashboards with the same panels for two setups:

- [`wgkex-prometheus.json`](../contrib/grafana/wgkex-prometheus.json): for a Prometheus datasource scraping the broker
  and worker `/metrics` endpoints.
- [`wgkex-influxdb.json`](../contrib/grafana/wgkex-influxdb.json): for an InfluxDB (1.x, InfluxQL) datasource, filled
  by Telegraf's `inputs.prometheus` plugin with `metric_version = 2` (measurement `prometheus`). See
  [`telegraf-wgkex.conf`](../contrib/grafana/telegraf-wgkex.conf); the dashboard expects the tag
  `wgkex_role = "worker"` on the worker inputs.

> [!NOTE]
> Histograms can't be aggregated into percentiles in InfluxQL, so the InfluxDB dashboard shows netlink latency as a
> mean.

Import them via *Dashboards → New → Import* and choose the datasource for the `DS` variable. The `worker` and `domain`
variables filter the broker panels; `instance` (Prometheus) / `url` (InfluxDB) filters the worker panels.

Panels cover effective peers against the weighted target, connected peers per domain, pending assignments,
online/draining state, assignments per worker, rebalancing hints and evictions, request rates and errors, rate limited
and blocked requests, worker queue depth, peer operations and netlink latency/reconnects. Rate limited and blocked
requests are only reported by brokers that support rate limiting and blocklists.
