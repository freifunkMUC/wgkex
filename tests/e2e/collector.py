"""Samples gateway, process and MQTT state of the e2e rig.

Outputs (JSON lines) in --out:
    samples.jsonl   every --interval seconds: per gateway WireGuard peers/handshakes, FDB entries,
                    routes and interface counters; CPU ticks of the rig processes; host CPU
    mqtt.jsonl      every MQTT message seen on the broker (topic, size, small payloads)
"""

import argparse
import json
import os
import subprocess
import threading
import time

import paho.mqtt.client as mqtt

CLK_TCK = os.sysconf("SC_CLK_TCK")


def run(*cmd: str) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        ).stdout
    except subprocess.TimeoutExpired:
        return ""


def gateway_sample(k: int) -> dict:
    ns = f"e2egw{k}"
    now = time.time()
    peers = with_endpoint = hs180 = hs150 = 0
    dump = run("ip", "netns", "exec", ns, "wg", "show", "wg-welt", "dump")
    for line in dump.splitlines()[1:]:
        f = line.split("\t")
        if len(f) < 8:
            continue
        peers += 1
        if f[2] != "(none)":
            with_endpoint += 1
        hs = int(f[4])
        if hs and now - hs < 180:
            hs180 += 1
        if hs and now - hs < 150:
            hs150 += 1
    links = {}
    try:
        for link in json.loads(run("ip", "-n", ns, "-s", "-j", "link", "show") or "[]"):
            name = link.get("ifname")
            if name in ("wg-welt", "vx-welt", "wan"):
                st = link.get("stats64", {})
                links[name] = {
                    "state": link.get("operstate"),
                    "rx": st.get("rx", {}),
                    "tx": st.get("tx", {}),
                }
    except json.JSONDecodeError:
        pass
    fdb = fdb_learned = 0
    for line in run("bridge", "-n", ns, "fdb", "show", "dev", "vx-welt").splitlines():
        if " dst " not in line:
            continue
        # all-zero entries are the per-peer flood (BUM replication) list
        if line.startswith("00:00:00:00:00:00"):
            fdb += 1
        else:
            fdb_learned += 1
    routes = len(
        run("ip", "-n", ns, "-6", "route", "show", "dev", "wg-welt").splitlines()
    )
    return {
        "peers": peers,
        "with_endpoint": with_endpoint,
        "hs180": hs180,
        "hs150": hs150,
        "fdb": fdb,
        "fdb_learned": fdb_learned,
        "routes": routes,
        "links": links,
    }


def proc_ticks(pid: int) -> dict | None:
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
        with open(f"/proc/{pid}/status") as f:
            rss = next(
                (int(line.split()[1]) for line in f if line.startswith("VmRSS:")), 0
            )
    except (FileNotFoundError, ProcessLookupError):
        return None
    fields = stat.rsplit(")", 1)[1].split()
    # fields[11..14] = utime stime cutime cstime (man 5 proc, counted from field 3)
    return {
        "utime": int(fields[11]),
        "stime": int(fields[12]),
        "cutime": int(fields[13]),
        "cstime": int(fields[14]),
        "rss_kb": rss,
    }


def host_cpu() -> dict:
    with open("/proc/stat") as f:
        cpu = f.readline().split()[1:]
    names = ["user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"]
    out = {n: int(v) for n, v in zip(names, cpu, strict=False)}
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                out["mem_available_kb"] = int(line.split()[1])
    return out


def mqtt_sniffer(host: str, port: int, out_path: str, stop: threading.Event) -> None:
    out = open(out_path, "a", buffering=1)
    lock = threading.Lock()

    def on_connect(client, userdata, flags, rc, properties=None):
        client.subscribe("#", qos=1)

    def on_message(client, userdata, msg):
        payload = msg.payload
        rec = {
            "t": time.time(),
            "topic": msg.topic,
            "len": len(payload),
            "qos": msg.qos,
            "retain": bool(msg.retain),
        }
        if len(payload) <= 256:
            rec["payload"] = payload.decode(errors="replace")
        with lock:
            out.write(json.dumps(rec) + "\n")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, "e2e-collector")
    client.on_connect = on_connect
    client.on_message = on_message
    while not stop.is_set():
        try:
            client.connect(host, port, keepalive=30)
            break
        except OSError:
            time.sleep(1)
    client.loop_start()
    stop.wait()
    client.loop_stop()
    out.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", required=True)
    p.add_argument("--gw", default="", help="gateways on this host, e.g. 1,2")
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--pids", required=True, help="JSON file role -> pid")
    p.add_argument("--mqtt-host", default="", help="sniff all MQTT traffic if set")
    p.add_argument("--mqtt-port", type=int, default=1883)
    args = p.parse_args()

    gws = [int(k) for k in args.gw.split(",") if k]
    stop = threading.Event()
    sniffer = None
    if args.mqtt_host:
        sniffer = threading.Thread(
            target=mqtt_sniffer,
            args=(
                args.mqtt_host,
                args.mqtt_port,
                os.path.join(args.out, "mqtt.jsonl"),
                stop,
            ),
            daemon=True,
        )
        sniffer.start()

    import signal

    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    samples = open(os.path.join(args.out, "samples.jsonl"), "a", buffering=1)
    while not stop.is_set():
        t = time.time()
        try:
            with open(args.pids) as f:
                pids = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pids = {}
        rec = {
            "t": t,
            "clk_tck": CLK_TCK,
            "host": host_cpu(),
            "gw": {f"gw{k}": gateway_sample(k) for k in gws},
            "proc": {role: proc_ticks(pid) for role, pid in pids.items()},
        }
        samples.write(json.dumps(rec) + "\n")
        stop.wait(max(0.0, args.interval - (time.time() - t)))
    if sniffer:
        sniffer.join(timeout=5)


if __name__ == "__main__":
    main()
