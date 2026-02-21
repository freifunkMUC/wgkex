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
- `netbox` section


## Design considerations

### Concentrator selection

Parker nodes call the config endpoint every `retry_interval` (default: 120s), regardless of whether they currently have connectivity or not.
Additionally, after a config change, they are offline for ~80s (two sleeps in noderoute.lua, 60s + 20s).
Nodes change active tunnel (from the current configuration) after the WireGuard handshakre times out (180s).

The retry interval can't be set too short, as otherwise it causes considerable load and "balance" issues, and possibly more frequent outages due to changed selected concentrators.
It can't be set too high because otherwise a node stays offline a long time when all its current gateways happen to go offline. It might also delay connectivity after reboot.

To reduce the amount of times that the selected concentrators change for a node, there a stickyness feature. On every request, the selected concentrators are saved in the IPAM alongside the Prefix information. On following requests, we first consider the stored concentrators, and only when a concentrator is more than `sticky_worker_tolerance`% peers over its calculated target, a node is redirected to a new concentrator with the most free capacity ("missing peers").

To make the peer counting work, the counts are interpolated between metric updates from the concentrators (same as with non-Parker /api/v2). The locally cached concentrator peer count is increased by 1 for each selected concentrator on each request.
This is only done when a concentrator has changed compared to the previous value compared in the IPAM, so we don't inflate the peer counts when a node won't actually create a new connection.

**Possible improvements:**
- Nodes send an indication whether they are currently offline or online. Then, interpolation could be further improved by bumping peer count if it was offline even if by chance the same concentrators have been selected, and decreasing peer count of the old gateway if it was online
- Nodes do active connectivity checks across the active or even both tunnels, e.g. every 10s:
  - Quicker switchover to backup tunnels
  - Immediately fetch config when all tunnels are inactive. This would also allow increasing `retry_interval` a lot
- Improve interpolation by increasing by number of broker instances (needs e.g. brokers announcing themselves over MQTT)
