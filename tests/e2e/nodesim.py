"""Simulates a fleet of Gluon nodes running checkuplink from cron.

Every node lives in its own network namespace (created by lib/net.sh). Like Gluon's micron entry

    * * * * * sleep $(awk 'BEGIN{srand();print int(rand()*40)}') && checkuplink

each node runs checkuplink once a minute after a 0-39 s delay. Busybox awk seeds srand() with the
current time in seconds, so all nodes whose cron fires in the same wall-clock second sleep for the
same "random" time. --jitter=gluon reproduces that (with a configurable per-node clock offset),
--jitter=uniform draws an independent delay per node.

checkuplink first runs is_connected(): an HTTP GET to the gateway's link address through the
tunnel with a 5 s timeout. That check is done natively here (cheap enough for thousands of nodes);
only if it fails the real reconnect path lib/node-checkuplink.sh is executed in the node namespace.
A lock per node prevents overlapping runs, like /var/lock/checkuplink.lock.

Outputs (JSON lines):
    events.jsonl     reconnect results and check failures per node
    nodes.jsonl      snapshots of the fleet state every --snapshot-interval seconds
"""

import argparse
import asyncio
import json
import os
import random
import signal
import socket
import time

CLONE_NEWNET = 0x40000000
NODE_IFACE = "wg_mesh_vpn"
HERE = os.path.dirname(os.path.abspath(__file__))


class Node:
    def __init__(self, idx: int, clock_offset: float) -> None:
        self.idx = idx
        self.ns = f"e2en{idx}"
        self.clock_offset = clock_offset
        self.busy = False
        self.gw: str | None = None
        self.gw_ll: str | None = None
        self.up = False
        self.last_ok = 0.0
        self.reconnects = 0


class Fleet:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.rng = random.Random(args.seed)
        self.nodes = [
            Node(i, abs(self.rng.gauss(0, args.clock_skew))) for i in range(args.nodes)
        ]
        self.gw_by_ip: dict[str, str] = {}
        with open(os.path.join(args.state, "site_peers")) as f:
            for line in f:
                name, hostport, _pub, ll = line.split()
                self.gw_by_ip[hostport.rsplit(":", 1)[0]] = name
        self.events = open(os.path.join(args.out, "events.jsonl"), "a", buffering=1)
        self.snapshots = open(os.path.join(args.out, "nodes.jsonl"), "a", buffering=1)
        self.root_ns = os.open("/proc/self/ns/net", os.O_RDONLY)
        self.checks = 0
        self.checks_failed = 0
        self.running = 0
        self.procs: set[asyncio.subprocess.Process] = set()
        self.stopping = False

    def event(self, **kw) -> None:
        kw.setdefault("t", time.time())
        self.events.write(json.dumps(kw) + "\n")

    def _socket_in_ns(self, node: Node) -> tuple[socket.socket, int] | None:
        """Creates a TCP socket inside the node's netns (sockets stay bound to their netns)."""
        fd = os.open(f"/run/netns/{node.ns}", os.O_RDONLY)
        try:
            os.setns(fd, CLONE_NEWNET)
            try:
                scope = socket.if_nametoindex(NODE_IFACE)
            except OSError:
                return None
            return socket.socket(socket.AF_INET6, socket.SOCK_STREAM), scope
        finally:
            os.setns(self.root_ns, CLONE_NEWNET)
            os.close(fd)

    async def is_connected(self, node: Node) -> bool:
        if not node.gw_ll:
            return False
        res = self._socket_in_ns(node)
        if res is None:
            return False
        sock, scope = res
        loop = asyncio.get_running_loop()
        sock.setblocking(False)
        try:
            async with asyncio.timeout(5):
                await loop.sock_connect(sock, (node.gw_ll, 80, 0, scope))
                await loop.sock_sendall(sock, b"GET / HTTP/1.0\r\n\r\n")
                data = await loop.sock_recv(sock, 64)
                return data.startswith(b"HTTP/1.0 200") or data.startswith(
                    b"HTTP/1.1 200"
                )
        except (OSError, TimeoutError):
            return False
        finally:
            sock.close()

    async def checkuplink(self, node: Node) -> None:
        if self.stopping:
            return
        if node.busy:
            # Lock file exists... exiting.
            return
        node.busy = True
        try:
            self.checks += 1
            if await self.is_connected(node):
                node.up = True
                node.last_ok = time.time()
                return
            self.checks_failed += 1
            was_up = node.up
            node.up = False
            self.event(
                ev="check_fail",
                node=node.idx,
                gw=node.gw,
                was_up=was_up,
                last_ok=node.last_ok or None,
            )
            await self.reconnect(node)
        finally:
            node.busy = False

    async def reconnect(self, node: Node) -> None:
        self.running += 1
        t0 = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                "ip",
                "netns",
                "exec",
                node.ns,
                "bash",
                os.path.join(HERE, "lib", "node-checkuplink.sh"),
                os.path.join(self.args.state, "nodes", str(node.idx)),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self.procs.add(proc)
            out, err = await proc.communicate()
            self.procs.discard(proc)
        finally:
            self.running -= 1
        result = {}
        for line in out.decode(errors="replace").splitlines():
            if line.startswith("RESULT "):
                for kv in line.split()[1:]:
                    k, _, v = kv.partition("=")
                    result[k] = v
        rc = proc.returncode
        host = result.get("host") or None
        gw = self.gw_by_ip.get(host) if host else None
        node.reconnects += 1
        ev = {
            "ev": "reconnect",
            "node": node.idx,
            "rc": rc,
            "api": result.get("api"),
            "gw": gw,
            "prev_gw": node.gw,
            "api_ms": int(result["api_ms"]) if result.get("api_ms") else None,
            "t0": t0,
            "dur": time.time() - t0,
        }
        if rc != 0:
            ev["log"] = err.decode(errors="replace")[-600:]
        self.event(**ev)
        if gw:
            node.gw = gw
            node.gw_ll = self._gw_ll(gw)
        if rc == 0:
            node.up = True
            node.last_ok = time.time()

    def _gw_ll(self, gw: str) -> str:
        with open(os.path.join(self.args.state, gw, "linklocal")) as f:
            return f.read().strip()

    def jitter(self, minute: int, node: Node) -> float:
        if self.args.jitter == "uniform":
            return self.rng.uniform(0, 40)
        # micron fires at the minute boundary, awk's srand() uses the wall clock second
        fire = minute + node.clock_offset
        return int(random.Random(int(fire)).random() * 40)

    async def cron(self, duration: float) -> None:
        loop = asyncio.get_running_loop()
        end = time.time() + duration
        minute = int(time.time() // 60 + 1) * 60
        while minute < end and not self.stopping:
            for node in self.nodes:
                at = minute + node.clock_offset + self.jitter(minute, node)
                loop.call_at(
                    loop.time() + max(0.0, at - time.time()),
                    lambda n=node: asyncio.ensure_future(self.checkuplink(n)),
                )
            minute += 60
            await asyncio.sleep(max(0.0, minute - time.time() - 1))

    async def snapshot_loop(self) -> None:
        while not self.stopping:
            by_gw: dict[str, int] = {}
            for node in self.nodes:
                if node.up and node.gw:
                    by_gw[node.gw] = by_gw.get(node.gw, 0) + 1
            snap = {
                "t": time.time(),
                "up": sum(by_gw.values()),
                "by_gw": by_gw,
                "busy": sum(1 for n in self.nodes if n.busy),
                "reconnecting": self.running,
                "checks": self.checks,
                "checks_failed": self.checks_failed,
            }
            if self.args.dump_assignments:
                snap["assign"] = [n.gw if n.up else None for n in self.nodes]
            self.snapshots.write(json.dumps(snap) + "\n")
            await asyncio.sleep(self.args.snapshot_interval)

    async def stop(self) -> None:
        self.stopping = True
        for proc in list(self.procs):
            try:
                proc.kill()
            except ProcessLookupError:
                pass


async def amain(args: argparse.Namespace) -> None:
    fleet = Fleet(args)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    keys = {}
    for node in fleet.nodes:
        with open(os.path.join(args.state, "nodes", str(node.idx), "publickey")) as f:
            keys[f.read().strip()] = node.idx
    with open(os.path.join(args.out, "keys.json"), "w") as f:
        json.dump(keys, f)
    fleet.event(ev="start", nodes=args.nodes, jitter=args.jitter)
    snap = asyncio.create_task(fleet.snapshot_loop())
    cron = asyncio.create_task(fleet.cron(args.duration))
    await asyncio.wait(
        [cron, asyncio.create_task(stop.wait())], return_when=asyncio.FIRST_COMPLETED
    )
    await fleet.stop()
    snap.cancel()
    cron.cancel()
    fleet.event(ev="stop")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--nodes", type=int, required=True)
    p.add_argument("--state", default=os.environ.get("E2E_STATE", "/run/wgkex-e2e"))
    p.add_argument("--out", required=True)
    p.add_argument("--duration", type=float, default=365 * 86400)
    p.add_argument("--jitter", choices=["gluon", "uniform"], default="gluon")
    p.add_argument(
        "--clock-skew",
        type=float,
        default=0.5,
        help="stddev (s) of the per-node clock offset, gluon jitter only",
    )
    p.add_argument("--snapshot-interval", type=float, default=5)
    p.add_argument("--dump-assignments", action="store_true")
    p.add_argument("--seed", type=int, default=1)
    args = p.parse_args()
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
