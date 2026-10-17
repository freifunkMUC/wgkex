"""Controller of the wgkex e2e rig: runs one scenario against one wgkex source tree.

Needs root on Linux with WireGuard, VXLAN and macvlan. Hosts:

    ctl     this host: mosquitto, the wgkex broker, the MQTT sniffer and this controller
    gw      one wgkex worker per gateway network namespace (--gw-ips, one entry per gateway)
    nodes   the simulated Gluon fleet (nodesim.py, one network namespace per node)

Without --gw-ips/--nodes-ip everything runs on this host. Otherwise the other hosts must run
``rigops.py agent`` (see run.sh) and the rig's L2 segment is a unicast VXLAN between the hosts.

Scenarios:
    baseline        bring-up of all nodes (key herd), then steady state
    reboot          bring-up, settle, hard reboot of gw1, observe redistribution
    drain-reboot    bring-up, settle, drain gw1 (SIGUSR1, phase D), wait, planned reboot, observe

Output: <out>/<run>/{marks.jsonl,meta.json,ctl/,gw*/,nodes/}; analyze.py summarizes it.
"""

import argparse
import base64
import copy
import io
import json
import os
import signal
import socket
import sys
import tarfile
import threading
import time

import paho.mqtt.client as mqtt
import yaml

from rigops import Local, Ops, Remote

HOST_IP = "10.0.0.1"
DOMAIN = "ffmuc_welt"


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def wait_port(host: str, port: int, timeout: float = 60) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection((host, port), 1).close()
            return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError(f"{host}:{port} did not come up")


class Rig:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.base = os.path.abspath(args.out)
        self.run_dir = os.path.join(self.base, args.run)
        os.makedirs(self.run_dir, exist_ok=True)
        self.marks = open(os.path.join(self.run_dir, "marks.jsonl"), "a", buffering=1)
        self.weights = [int(w) for w in args.weights.split(",")]
        self.n_gw = len(self.weights)
        self.overlay = {}
        for path in args.config_overlay or []:
            with open(path) as f:
                self.overlay = deep_merge(self.overlay, yaml.safe_load(f) or {})
        self.src = os.path.abspath(args.src)

        gw_ips = args.gw_ips.split() if args.gw_ips else []
        self.multi = bool(gw_ips)
        if self.multi:
            assert len(gw_ips) == self.n_gw, "--gw-ips needs one address per gateway"
            assert args.nodes_ip and args.ctl_ip
            self.net_env = {
                "E2E_CTL_IP": args.ctl_ip,
                "E2E_GW_IPS": " ".join(ip.split(":")[0] for ip in gw_ips),
                "E2E_NODES_IP": args.nodes_ip.split(":")[0],
            }
        else:
            gw_ips = ["local"] * self.n_gw
            self.net_env = {}
        self.net_env.update(
            E2E_GATEWAYS=str(self.n_gw), E2E_STATE=args.state, E2E_VNI=str(4325)
        )

        # host -> gateways on it
        self.gw_hosts: dict[str, list[int]] = {}
        for k, ip in enumerate(gw_ips, start=1):
            self.gw_hosts.setdefault(ip, []).append(k)
        self.ctl = Local(Ops(self.base))
        self.hosts: dict[str, tuple] = {"ctl": (self.ctl, args.ctl_ip)}
        self.gw_of: dict[int, object] = {}
        for ip, ks in self.gw_hosts.items():
            role = "gw" + "_".join(str(k) for k in ks)
            h = Remote(ip) if self.multi else Local(Ops(self.base))
            self.hosts[role] = (h, ip)
            for k in ks:
                self.gw_of[k] = h
        self.nodes = Remote(args.nodes_ip) if self.multi else Local(Ops(self.base))
        self.hosts["nodes"] = (self.nodes, args.nodes_ip)

    # --- helpers -----------------------------------------------------------------------------

    def mark(self, name: str, **kw) -> None:
        kw.update(t=time.time(), mark=name)
        self.marks.write(json.dumps(kw) + "\n")
        log(f"MARK {name} {kw}")

    def config(self, worker: int | None = None) -> str:
        cfg = {
            "domains": [DOMAIN],
            "domain_prefixes": ["ffmuc_"],
            "workers": {
                f"gw{k}": {"weight": w} for k, w in enumerate(self.weights, start=1)
            },
            "mqtt": {
                "broker_url": HOST_IP,
                "broker_port": 1883,
                "username": "",
                "password": "",
                "keepalive": 5,
                "tls": False,
            },
            "broker_listen": {"host": HOST_IP, "port": 5000},
            "log_level": "INFO",
            "logging_config": {
                "version": 1,
                "formatters": {
                    "standard": {
                        "format": "%(asctime)s.%(msecs)03d %(levelname)-8s "
                        "[%(filename)s:%(lineno)d] %(message)s",
                        "datefmt": "%Y-%m-%dT%H:%M:%S",
                    }
                },
                "handlers": {
                    "console": {
                        "class": "logging.StreamHandler",
                        "formatter": "standard",
                    }
                },
                "root": {"handlers": ["console"], "level": "INFO"},
            },
            "load_balancing": {
                "rebalancing": {"enabled": self.args.rebalancing == "on"}
            },
        }
        if worker is not None:
            cfg["externalName"] = f"10.0.0.{10 + worker}"
        return yaml.safe_dump(deep_merge(cfg, self.overlay), sort_keys=False)

    def wait_workers_online(self, workers: list[str], timeout: float = 180) -> float:
        """Waits until the workers are online and published a connected peers metric."""
        status: dict[str, int] = {}
        metrics: set[str] = set()
        done = threading.Event()

        def on_message(client, userdata, msg):
            parts = msg.topic.split("/")
            try:
                if parts[0] == "wireguard-worker" and parts[-1] == "status":
                    status[parts[1]] = int(msg.payload)
                elif parts[0] == "wireguard-metrics" and len(parts) >= 3:
                    if int(msg.payload) >= 0:
                        metrics.add(parts[2])
                    else:
                        metrics.discard(parts[2])
            except ValueError:
                return
            if all(status.get(w) == 1 and w in metrics for w in workers):
                done.set()

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, f"e2e-wait-{time.time()}"
        )
        client.on_message = on_message
        client.connect(HOST_IP, 1883)
        client.subscribe([("wireguard-worker/+/status", 1), ("wireguard-metrics/#", 1)])
        client.loop_start()
        t0 = time.time()
        ok = done.wait(timeout)
        client.loop_stop()
        client.disconnect()
        if not ok:
            raise RuntimeError(f"workers not online: status={status} metrics={metrics}")
        return time.time() - t0

    def sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            dead = {r: c for r, c in self.ctl.alive().items() if c is not None}
            if dead:
                raise RuntimeError(f"died: {dead}")
            time.sleep(min(10, max(0, end - time.time())))

    # --- setup -------------------------------------------------------------------------------

    def setup(self) -> None:
        a = self.args
        for role, (h, ip) in self.hosts.items():
            env = dict(self.net_env)
            if self.multi:
                env["E2E_LOCAL_IP"] = ip.split(":")[0]
            log(f"{role}: prepare {ip or 'local'} {h.ping()}")
            h.prepare(a.run, role, env, self.src)
            h.net("teardown")
        self.ctl.net("core_up")
        self.ctl.net("host_up")
        self.ctl.net("host_neigh", str(a.nodes))
        self.ctl.net("underlay_fdb", str(a.nodes))
        for ip, ks in self.gw_hosts.items():
            h = self.gw_of[ks[0]]
            h.net("core_up")
            for k in ks:
                h.net("gw_create", str(k))
                h.net("gw_neigh", str(k), str(a.nodes))
            h.net("underlay_fdb", str(a.nodes))
        t = time.time()
        self.nodes.net("core_up")
        self.nodes.net("site_peers")
        self.nodes.net("nodes_create", str(a.nodes))
        self.nodes.net("underlay_fdb", str(a.nodes))
        self.mark("setup_done", nodes_create_s=round(time.time() - t, 1))

    def start_services(self) -> None:
        a = self.args
        self.ctl.start_mosquitto()
        wait_port(HOST_IP, 1883)
        self.ctl.start_broker(self.config())
        wait_port(HOST_IP, 5000)
        for k, h in self.gw_of.items():
            h.start_gateway(k, self.config(k), a.bum_pps)
        self.ctl.start_collector([], HOST_IP)
        for ip, ks in self.gw_hosts.items():
            self.gw_of[ks[0]].start_collector(ks)
        self.nodes.start_collector([])
        took = self.wait_workers_online([f"gw{k}" for k in self.gw_of])
        self.mark("workers_online", took=round(took, 1))

    # --- scenario ----------------------------------------------------------------------------

    def boot(self, k: int) -> None:
        h = self.gw_of[k]
        h.net("gw_ifaces_up", str(k))
        h.net("gw_wan", str(k), "up")
        h.start_gateway(k, self.config(k), self.args.bum_pps)
        self.mark("gw_back", gw=f"gw{k}")
        took = self.wait_workers_online([f"gw{k}"])
        self.mark("gw_online", gw=f"gw{k}", took=round(took, 1))

    def hard_reboot(self, k: int) -> None:
        h = self.gw_of[k]
        self.mark("reboot", gw=f"gw{k}", kind="hard")
        # Link first: MQTT only notices through the keepalive timeout, like a real power cut
        h.net("gw_wan", str(k), "down")
        h.stop_gateway(k, int(signal.SIGKILL))
        h.net("gw_ifaces_down", str(k))
        self.sleep(self.args.downtime)
        self.boot(k)

    def drain_reboot(self, k: int) -> None:
        h = self.gw_of[k]
        self.mark("drain", gw=f"gw{k}")
        # Phase D: SIGUSR1 makes the worker publish status 2 (draining)
        h.signal_worker(k, int(signal.SIGUSR1))
        self.sleep(self.args.drain_wait)
        self.mark("reboot", gw=f"gw{k}", kind="planned")
        h.stop_gateway(k, int(signal.SIGTERM))
        h.net("gw_wan", str(k), "down")
        h.net("gw_ifaces_down", str(k))
        self.sleep(self.args.downtime)
        self.boot(k)

    def run(self) -> None:
        a = self.args
        meta = {k: v for k, v in vars(a).items() if k not in ("config_overlay",)}
        meta.update(
            weights=self.weights,
            overlay=self.overlay,
            kernel=os.uname().release,
            cpus=os.cpu_count(),
            hosts={r: ip for r, (_, ip) in self.hosts.items()},
        )
        commit_file = os.path.join(self.src, ".commit")
        if os.path.exists(commit_file):
            with open(commit_file) as f:
                meta["commit"] = f.read().strip()
        with open(os.path.join(self.run_dir, "meta.json"), "w") as f:
            json.dump(meta, f, indent=2)

        self.setup()
        self.start_services()
        self.mark("nodes_start", nodes=a.nodes)
        self.nodes.start_nodesim(a.nodes, a.jitter, a.dump_assignments)

        t0 = time.time()
        snap = {}
        while time.time() - t0 < a.warmup_max:
            self.sleep(10)
            snap = self.nodes.last_snapshot()
            if snap.get("up", 0) >= a.nodes * 0.995:
                break
        self.mark("warm", up=snap.get("up"), took=round(time.time() - t0, 1))

        if a.scenario == "baseline":
            self.sleep(a.steady)
        elif a.scenario == "reboot":
            self.sleep(a.settle)
            self.hard_reboot(1)
            self.sleep(a.observe)
        elif a.scenario == "drain-reboot":
            self.sleep(a.settle)
            self.drain_reboot(1)
            self.sleep(a.observe)
        self.mark("end")

    def shutdown(self) -> None:
        log("shutting down")
        order = ["nodes"] + [r for r in self.hosts if r.startswith("gw")] + ["ctl"]
        for role in order:
            h, _ = self.hosts[role]
            try:
                h.shutdown(keep_network=self.args.keep_network)
            except Exception as e:
                log(f"{role}: shutdown failed: {e!r}")
        if not self.multi:
            return
        for role, (h, ip) in self.hosts.items():
            if role == "ctl":
                continue
            try:
                data = base64.b64decode(h.results(self.args.run))
                with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                    tar.extractall(self.run_dir, filter="data")
            except Exception as e:
                log(f"{role}: fetching results failed: {e!r}")


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--scenario", choices=["baseline", "reboot", "drain-reboot"])
    p.add_argument("--run", required=True, help="run name (output subdirectory)")
    p.add_argument("--src", required=True, help="wgkex source tree (repo root)")
    p.add_argument("--out", required=True)
    p.add_argument("--label", default="")
    p.add_argument("--state", default=os.environ.get("E2E_STATE", "/run/wgkex-e2e"))
    p.add_argument("--ctl-ip", default="", help="underlay address of this host")
    p.add_argument("--gw-ips", default="", help="underlay host of each gateway")
    p.add_argument("--nodes-ip", default="", help="underlay host of the node fleet")
    p.add_argument("--nodes", type=int, default=2000)
    p.add_argument("--weights", default="30,30,20,20", help="one per gateway")
    p.add_argument("--rebalancing", choices=["on", "off"], default="off")
    p.add_argument(
        "--config-overlay",
        action="append",
        help="YAML merged into broker and worker configs (repeatable)",
    )
    p.add_argument("--jitter", choices=["gluon", "uniform"], default="gluon")
    p.add_argument("--bum-pps", type=float, default=2)
    p.add_argument("--warmup-max", type=float, default=900)
    p.add_argument("--steady", type=float, default=900)
    p.add_argument("--settle", type=float, default=300)
    p.add_argument("--downtime", type=float, default=120)
    p.add_argument("--observe", type=float, default=1800)
    p.add_argument("--drain-wait", type=float, default=300)
    p.add_argument("--dump-assignments", action="store_true")
    p.add_argument("--keep-network", action="store_true")
    args = p.parse_args()

    rig = Rig(args)

    def on_sigterm(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_sigterm)
    ok = False
    try:
        rig.run()
        ok = True
    except KeyboardInterrupt:
        rig.mark("aborted")
    except Exception as e:
        rig.mark("error", error=repr(e))
        raise
    finally:
        rig.shutdown()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
