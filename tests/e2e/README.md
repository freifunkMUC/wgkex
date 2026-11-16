# wgkex end-to-end test rig

Runs the real broker and workers against ~2000 simulated Gluon nodes that follow
`ffmuc-mesh-vpn-wireguard-vxlan`'s `checkuplink` logic, with kernel WireGuard and VXLAN, and measures
peer distribution, reconnects, key throughput, CPU and BUM replication. Not part of the unit tests
(bazel ignores `tests/`, ruff only checks `wgkex/`).

## Topology

```
ctl host (root ns)          gateway hosts                      node host
mosquitto :1883             netns e2egwK (K=1..4)              netns e2enI (I=0..N-1)
broker 10.0.0.1:5000        wan 10.0.0.(10+K)                  wan 10.1.x.y
                            wg-welt :51820 + vx-welt (8472)    wg-welt -> gateway, vx-welt over it
             \______________ e2e-wan: one flat L2 "internet" ______________/
```

* `e2e-wan` is a dummy device on a single host, or a unicast VXLAN (VNI 77, UDP 4789, static FDB,
  no learning) between hosts when `E2E_LOCAL_IP` is set, so it works in clouds without multicast.
  All MACs and IPs are deterministic and neighbours are static, so there is no ARP or flooding.
* Each gateway runs the unmodified worker (`python -m wgkex.worker.app`) in its netns with UTS
  hostname `gwK`; gateway keys are deterministic (`sha256("wgkex-e2e-gwK")`). Weights 30/30/20/20.
* Each node is a netns with a real `wg-welt`/`vx-welt` pair. `nodesim.py` schedules checks every
  60 s plus jitter; a check is "is the gateway's link-local reachable" (5 s timeout). On failure it runs
  `lib/node-checkuplink.sh`, a port of `checkuplink` (v2 POST via busybox wget, v1 fallback with a
  random site peer, rebuild WG + VXLAN, FDB entry).
* `lib/gw_http.py` answers HTTP on each gateway's link-local; `lib/bum.py` sends a small broadcast
  rate into each node's VXLAN so per-gateway BUM replication (`vx-welt`/`wg-welt` tx pps, drops for
  peers that are configured but not connected) can be measured.
* `collector.py` samples per-gateway peers/connected peers/FDB/routes, interface counters, CPU per
  process and host, and sniffs MQTT. `analyze.py` turns everything into `summary.{md,json}` and
  `timeline.png` per run.

### Approximations

* checkuplink: NTP, ip6tables, batman-adv and DNS are skipped; `jq` replaces the Lua JSON parsing,
  busybox `wget` replaces `uclient-fetch`. Endpoint, port, public key, link-local and VNI handling
  follow the original.
* `--jitter gluon` (default) reproduces busybox awk `srand()` seeding with the current second, so
  nodes that fire in the same second pick the same jitter and herd together (this is real behaviour
  on routers). `--jitter uniform` is available for comparison.
* Underlay MTU (1450 on the VXLAN wan) is not adjusted; test traffic uses small packets only.
* A hard reboot is: wan down, SIGKILL the worker, delete `wg-welt`/`vx-welt`, wait, recreate, start
  the worker. The killed worker never publishes status 0, as with a real power loss.
* All simulated nodes reach the broker from one IP, so the rig sets
  `rate_limit.per_client_requests: 0`; the global limit stays at its default.
* Brokers before #304 can miss their MQTT subscriptions (flask_mqtt connected before the
  `on_connect` handler was registered); every v2 request then gets "No worker online". Such runs
  show up under **warnings**.

## Usage

Needs root, Debian/Ubuntu, kernel WireGuard. `setup_host.sh` installs everything into
`/opt/wgkex-e2e/venv`. Put the wgkex trees to test in `src/<name>/` (e.g. `git archive`), then:

```sh
# everything on one host (50 nodes is a quick smoke test)
NODES=50 ./run.sh local smoke

# multi host: on every gateway/node host first
sudo ./run.sh agent <CTL_IP>
# then on the broker host (one IP per gateway; repeat an IP to stack gateways on one host)
sudo ./run.sh controller "<GW1_IP> <GW2_IP> <GW3_IP> <GW4_IP>" <NODES_IP> compare
# or in the background: ./run.sh controller-bg ... and poll ./run.sh status
```

Agents listen on TCP 7700 and only accept the controller's IP. The controller pulls every host's
results into `results/` at the end of each run.

A plan (`plans/*.txt`) is a list of `name src scenario-args...`; see `python3 scenario.py --help`.
Scenarios:

| scenario | timeline |
|---|---|
| `baseline` | start 4 workers, start all nodes (key herd), wait for all up, steady window |
| `reboot` | baseline, settle, hard reboot gw1 for `--downtime`, observe |
| `drain-reboot` | baseline, settle, SIGUSR1 to gw1's worker (drain, phase D), wait `--drain-wait`, then reboot |

`--rebalancing on|off` sets `load_balancing.rebalancing.enabled` on broker and workers;
`--config-overlay FILE` deep-merges YAML into the configs (e.g. `overlays/publish-all.yaml`).

## Metrics (summary.md)

* **t99 up**: seconds from node start until 99 % of nodes have a working uplink.
* **keys/s peak**: MQTT key publishes per second; **processed/s** per gateway from
  `Processing queue for key` log lines; **lag**: publish → processed.
* **max dev / converge**: max relative deviation of connected peers per gateway from the weighted
  target; time after the gateway comes back until it stays within 10 % (20 %), `n/c` if it never
  does; rebooted gateway's share after 5/15/30/60 min.
* **reconnects / outages**: checkuplink reconnects after warm-up, outage = last good check →
  reconnected (includes the 1 min check interval), split by cause (reboot / rebalance eviction).
* **BUM**: per gateway, configured peers, FDB flood entries, `wg-welt` tx pps and tx drops
  (replication to configured-but-absent peers) in the steady window.
* **CPU**: per process (broker, mosquitto, workers) and host softirq.
* **rate limited**: `wgkex_broker_key_exchange_requests_total{result="rate_limited"}` from the
  broker's `/metrics` at the end of the run (brokers with #299 and later).
* **warnings**: "No worker online" answers, v1 fallbacks and node simulator
  errors. A run with warnings does not show wgkex's normal behaviour.
