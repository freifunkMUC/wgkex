# Operating wgkex

This guide covers running wgkex in production. All options are documented in
[`wgkex.yaml.example`](../wgkex.yaml.example); the API is specified in [`openapi.yaml`](openapi.yaml). Metrics and
dashboards are described in [metrics.md](metrics.md).

- [Load balancing](#load-balancing)
  - [Gateway selection](#gateway-selection)
  - [Targeted publishing](#targeted-publishing)
  - [Location preference](#location-preference)
  - [Active rebalancing](#active-rebalancing)
  - [Multiple broker instances](#multiple-broker-instances)
- [Drain mode and planned reboots](#drain-mode-and-planned-reboots)
  - [Planned reboot runbook](#planned-reboot-runbook)
- [Worker peer handling](#worker-peer-handling)
- [Rate limiting and key lists](#rate-limiting-and-key-lists)
- [Running behind a reverse proxy](#running-behind-a-reverse-proxy)
- [Logging](#logging)

## Load balancing

### Gateway selection

For `/api/v2`, the broker chooses among the workers that are online for the requested domain and have published their
connection data. Each worker gets a target share of all peers according to its `weight` in the `workers` config.
Workers missing from the `workers` config get the average weight (and a warning is logged).

The worker is picked randomly, with a probability proportional to how far it is below its target. Randomization avoids
that all clients reconnecting after a gateway outage (or talking to different broker instances) land on the same
worker.

Workers only report peers with a recent handshake as connected. To bridge the time until a newly assigned peer shows up
in these metrics, the broker counts recent assignments as pending load of the chosen worker
(`load_balancing.pending_assignment_ttl`, default 300 s; an assignment is kept for at least
`pending_assignment_settle_time`, default 60 s, before a metric update may supersede it).

Workers only announce themselves as online once all their WireGuard interfaces are ready. A worker is treated as
offline for a domain if the broker did not receive a connected peers metric for it within
`load_balancing.metrics_max_age` seconds (default 180, i.e. three metrics intervals; `0` disables the check).

### Targeted publishing

Keys received via `/api/v1` are published to all workers (the client picks a gateway itself). Keys received via
`/api/v2` are only published to the chosen worker (`load_balancing.targeted_publish`, enabled by default).

When a key moves to a different worker, the broker asks the previous worker to remove the peer immediately
(`wireguard-worker/{worker}/{domain}/remove`) instead of waiting for the stale peer cleanup. The last assignment of a
key is remembered for `previous_assignment_ttl` (default 21600 s) and up to `previous_assignment_max_entries` (default
100000) keys.

> [!NOTE]
> Older workers subscribe to `wireguard/{domain}/+` and add every key; newer workers only act on keys for `all` and
> their own hostname. Older brokers publish every key to `all`. Mixed versions therefore keep working; the bandwidth
> savings only apply once both sides are updated.

### Location preference

If the client sends a `location` and workers are configured with that `location`, these are preferred as long as their
effective peers stay within `target + max(location_min_excess_peers, location_tolerance * target)` (defaults `10` and
`0.1`). Beyond that, and for clients without or with an unknown location, the normal selection applies.

> [!WARNING]
> Keep `location_tolerance` at or below `rebalancing.tolerance`, otherwise rebalancing moves these peers away again.

### Active rebalancing

Clients stay connected to their gateway as long as the connection works, so after a gateway reboot the load would stay
uneven. With `load_balancing.rebalancing.enabled` on the broker **and** the workers, the broker periodically (every
`interval`, default 60 s) tells overloaded workers how many peers they have above their target, once the set of online
workers has been stable for `stable_time` (default 600 s). Excess within `tolerance` (default `0.1`) or
`min_excess_peers` (default `10`) is tolerated.

These workers then remove a small, rate-limited number of their peers per interval (`max_evictions_ratio`, default
`0.01`, at least one peer), preferring the most recently (re-)connected ones and never evicting the same peer again
within `eviction_cooldown` (default 3600 s). The affected clients notice the broken connection and request a new
gateway via the API within a minute or two.

### Multiple broker instances

When running multiple broker instances behind a load balancer, enable `load_balancing.share_pending_assignments` on all
of them. Each assignment is then published on `wireguard-broker/{broker_id}/assignment` and counted as pending by the
other brokers as well. `broker_id` defaults to the hostname plus a random suffix.

## Drain mode and planned reboots

A worker can be put into drain mode before planned maintenance:

- `kill -USR1 <pid>` (or `docker kill -s USR1 <container>`) starts draining, `kill -USR2 <pid>` stops it.
- Alternatively configure `drain.file` (e.g. `/run/wgkex/drain`): the worker is draining while that file exists,
  checked every `drain.check_interval` seconds (default 5).

> [!WARNING]
> Put the drain file in a non-persistent location like `/run`, so the worker does not come back up draining after the
> reboot. `SIGUSR2` does not override an existing drain file.

A draining worker publishes status `2`. The broker then stops assigning new `/api/v2` clients to it (unless every
worker of a domain is draining; clients always get an endpoint). If active rebalancing is enabled on broker and worker,
the broker also asks the draining worker to evict its connected peers at `load_balancing.rebalancing.drain_rate`
(default 10 %) of its peers per `interval`, without waiting for `stable_time`, and only in domains where another
worker that is not draining is online. Evicted nodes notice the broken tunnel within about 1–2 minutes and redo the
key exchange, which assigns them to another worker. Brokers from before drain mode treat status `2` like `1`.

On `SIGINT` or `SIGTERM` (e.g. `docker stop`, systemd), the worker marks itself offline (status `0`) at the broker
before exiting. If it is killed, its MQTT last will does the same.

### Planned reboot runbook

1. Start draining: `docker kill -s USR1 <worker container>` or `touch /run/wgkex/drain`. The worker logs
   `Worker is draining, published status 2`.
2. Wait until the connected peers metric (`wireguard-metrics/{domain}/{worker}/connected_peers`, or
   `wgkex_broker_worker_connected_peers` in Prometheus) is close to 0. With the defaults (`interval: 60`,
   `drain_rate: 0.1`) roughly 90 % of the peers are gone after 20 minutes. Without rebalancing, peers stay until the
   reboot, but no new ones arrive.
3. Reboot. On shutdown (`SIGTERM`) the worker publishes status `0`; the last will does the same if it is killed.
4. After the boot, the worker publishes status `1` once its WireGuard interfaces are ready (a drain file in `/run` is
   gone, signals are not persisted). If a drain file in a persistent location was used, delete it.
5. Normal rebalancing (if enabled) moves peers back to the rebooted worker after `stable_time`.

## Worker peer handling

- **Stale peer cleanup:** peers without a handshake for `peer_cleanup.stale_timeout` seconds (default 1800) are
  removed every `peer_cleanup.interval` seconds (default 300). Peers that were just added are kept until they had time
  to connect.
- **Queue:** queued key operations are picked up immediately (blocking queue, processed in batches of up to 256).
- **Netlink:** each worker thread keeps its netlink sockets and interface indexes open instead of reconnecting per
  operation. Broken sockets are reopened and recreated interfaces looked up again automatically. WireGuard peers are
  still added one per netlink message, as pyroute2's `WireGuard.set()` accepts only a single peer.
- **Reporting:** the worker publishes its endpoint data per domain and the number of connected peers every 60 seconds.

## Rate limiting and key lists

Both key exchange endpoints are rate limited per client address and globally (`rate_limit`, defaults: 30 requests per
60 s per client, 6000 requests per 60 s globally, IPv6 clients grouped per `ipv6_prefix_length` = /64, at most
`max_tracked_clients` = 100000 tracked clients; `0` disables a limit).

> [!IMPORTANT]
> A limited request still gets the normal `200` response, because Gluon clients fall back to `/api/v1` and a random
> gateway on errors. The key is just not published again, and `/api/v2` answers with the worker the key was already
> assigned to (if it is still online), so repeated requests are a no-op.

Key lists (`key_lists`) contain public keys inline or in files with one key per line (`#` starts a comment):

- **Blocklist** (broker: `blocklist` / `blocklist_file`): keys are rejected with `403` and never published. Keys newly
  added to the file are also removed from all workers.
- **Allowlist** (worker: `allowlist` / `allowlist_file`): keys are never removed from a worker by the stale peer
  cleanup, rebalancing or draining.

Files are reloaded without a restart within `reload_interval` seconds (default 60) after being modified. If a file is
deleted or unreadable, its previous keys stay active; empty it instead.

## Running behind a reverse proxy

By default, the client address is the TCP peer address. Behind a reverse proxy that would be the proxy for every
request, so set `broker.trusted_proxies` to take it from `X-Forwarded-For`. The header can be spoofed by any client,
so it is only used from trusted proxies:

- **A list of proxy addresses/networks** (e.g. `["127.0.0.1/32", "::1/128"]`): the header is only used if the TCP peer
  is in one of them. It is evaluated from the right, skipping trusted proxies, and the first other address is the
  client. Entries further left were sent by the client and are ignored.
- **A number `n`** of proxies every request passes through: the `n`-th entry from the right is used, like werkzeug's
  `ProxyFix(x_for=n)`. Only use this if the broker can't be reached without passing the proxies.

```yaml
broker:
  trusted_proxies: ["127.0.0.1/32", "::1/128"]
  log_client_ip: true
```

The resolved address is used for rate limiting and, with `broker.log_client_ip: true` (off by default), it is logged
at `INFO` level with the domain and key of every key exchange request (`..., Client: 192.0.2.1`).

> [!WARNING]
> The broker's `GET /metrics` endpoint is served on the same listener as the API. Block `/metrics` at the proxy or set
> `metrics.broker_endpoint: false` if worker loads should not be public.

## Logging

The log level is set with `log_level` (default `INFO`). A full Python logging configuration in `dictConfig` format can
be given in `logging_config`; `log_level` overrides its root level.
