 # Parker

This contains some information and explanations specific to running wgkex for [Parker](https://www.freifunk-bs.de/parker.html)-style Freifunk networks.

## Features

- API-compatible with Parker nodes as per Freifunk Braunschweig firmware (caveat: only 464XLAT mode works)
- 464XLAT support
- Supports a PoP concept, configures one concentrator tunnel per PoP
- Concentrators selected dynamically, but existing concentrators reused unless over treshold (see [Design Considerations](#design-considerations))
- NetBox as IPAM backend to allocate node prefixes (by pubkey) and store additional data


## Configuration

For Parker the following configuration items are relevant in addition to the default options, see the `wgkex.yaml.example` for explanations:

- `workers` map (including `pop` key)
- `sticky_worker_tolerance`
- `broker_signing_key`
- `parker` section
- `peer_cleanup` section (`parker_stale_timeout`, `initial_handshake_grace`; the older `cleanup` section is still
  accepted as an alias)
- `netbox` section

Parker uses the same broker protections as `/api/v2`: the key blocklist (`403`), rate limiting, `trusted_proxies` and
`log_client_ip`, and the `wgkex_broker_key_exchange_requests{api="v3"}` and `wgkex_broker_assignments{domain="parker"}`
metrics. Unlike v1/v2, rate limited v3 requests are answered with `429`, since Parker nodes have no older API to fall
back to and simply retry after `retry_interval`. See [`openapi.yaml`](openapi.yaml) for the request and response
format.

## Interaction with the other worker features

A Parker worker serves the single `wg-nodes` interface and no domains, so:

- **MQTT:** the broker publishes a node's peer only to the concentrators chosen for it (`parker/wireguard/{worker}`,
  QoS 1), and sends the same message with `"Remove": true` to concentrators the node was moved away from. Workers also
  still accept messages addressed to `all`.
- **Drain mode:** works like for the other workers (`SIGUSR1`/`SIGUSR2` or the drain file). A draining concentrator
  (status `2`) is neither chosen nor kept as a sticky concentrator, unless all concentrators of the PoP are draining.
  Nodes move away from it on their next config request.
- **Rebalancing** (`load_balancing.rebalancing`) is not used for Parker workers, they get no rebalance hints. Load is
  balanced by the concentrator selection on every config request instead.
- **Stale peer cleanup:** Parker peers use `peer_cleanup.parker_stale_timeout` and `initial_handshake_grace` (see
  below). The key allowlist also protects Parker peers from the cleanup.
- **Netlink:** Parker uses the same long-lived netlink handles as the other workers.


## Design considerations

### Offline peer cleanup

The Parker worker defaults to a 300-second stale-handshake timeout and a
600-second initial-handshake grace. The stale timeout accommodates the default
120-second config retry and 180-second WireGuard tunnel timeout. Zero or missing
handshake timestamps mean that a peer has never handshaked; they do not cause
immediate deletion. Accepted queue updates refresh an in-memory, bounded
provisioning tracker, and cleanup serializes only with updates for the same
public key or assigned prefix.

WireGuard does not expose peer creation time. After a worker restart, every
untracked never-handshaked peer therefore receives one grace period from worker
startup. Truly abandoned peers are removed on a later sweep rather than retained
forever. The tracker retains at most 65,536 peer timestamps; if that bound is
reached before entries age out, new provisioning fails explicitly rather than
dropping grace state and risking premature cleanup.

For a stale Parker peer, cleanup removes the selected assigned-prefix route
before removing the WireGuard peer. If another current peer owns the prefix, its
route is preserved. A real route or peer failure is reported and retried on the
next sweep; already-absent state is treated as idempotent.

When an accepted queue update reassigns a peer, the worker locks both the old
and new prefixes, installs the new peer state and route, then removes the old
route. `Range6` must match the configured Parker IPv6 allocation prefix length.
If old-route deletion fails after reassignment, the bounded worker-side tracker
retries it on cleanup sweeps after checking that no current peer owns it. This
prevents cleanup from preserving a route that the peer immediately abandons.

### Concentrator selection

Parker nodes call the config endpoint every `retry_interval` (default: 120s), regardless of whether they currently have connectivity or not.
Additionally, after a config change, they are offline for ~80s (two sleeps in noderoute.lua, 60s + 20s).
Nodes change active tunnel (from the current configuration) after the WireGuard handshakre times out (180s).

The retry interval can't be set too short, as otherwise it causes considerable load and "balance" issues, and possibly more frequent outages due to changed selected concentrators.
It can't be set too high because otherwise a node stays offline a long time when all its current gateways happen to go offline. It might also delay connectivity after reboot.

To reduce the amount of times that the selected concentrators change for a node, there is a stickyness feature. On every request, the selected concentrators are saved in the IPAM alongside the Prefix information. On following requests, we first consider the stored concentrators, and only when a concentrator is more than `sticky_worker_tolerance`% peers over its calculated target, a node is redirected to a new concentrator with the most free capacity ("missing peers").

To make the peer counting work, newly selected concentrators are counted as pending assignments until the concentrator's metrics include them (same as with non-Parker /api/v2, see `load_balancing.pending_assignment_*`).
This is only done when a concentrator has changed compared to the previous value stored in the IPAM, so we don't inflate the peer counts when a node won't actually create a new connection.

**Possible improvements:**
- Nodes send an indication whether they are currently offline or online. Then, interpolation could be further improved by bumping peer count if it was offline even if by chance the same concentrators have been selected, and decreasing peer count of the old gateway if it was online
- Nodes do active connectivity checks across the active or even both tunnels, e.g. every 10s:
  - Quicker switchover to backup tunnels
  - Immediately fetch config when all tunnels are inactive. This would also allow increasing `retry_interval` a lot
- Share Parker pending assignments between broker instances, like `load_balancing.share_pending_assignments` does for /api/v2
