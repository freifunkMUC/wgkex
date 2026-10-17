"""Summarizes e2e rig runs (output of scenario.py) into summary.json, summary.md and graphs.

Usage: analyze.py RESULTS_DIR      (every subdirectory with a meta.json is one run)
"""

import argparse
import datetime
import glob
import json
import os
import re
import statistics
import sys

LOG_TS = re.compile(r"^(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)[.,](\d{3})")
PROCESS = re.compile(r"Processing queue for key (\S+) on domain")
EVICT = re.compile(r"Rebalancing: evicting peer (\S+) from")
REMOVE = re.compile(r"Removing peer (\S+) on domain")


def jsonl(path: str):
    try:
        with open(path) as f:
            for line in f:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return


def log_lines(path: str):
    """Yields (unix time, line) of a wgkex log written with TZ=UTC."""
    try:
        f = open(path, errors="replace")
    except FileNotFoundError:
        return
    with f:
        for line in f:
            m = LOG_TS.match(line)
            if not m:
                continue
            dt = datetime.datetime.fromisoformat(f"{m[1]}T{m[2]}").replace(
                tzinfo=datetime.timezone.utc
            )
            yield dt.timestamp() + int(m[3]) / 1000, line


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


def rnd(v, n=1):
    return None if v is None else round(v, n)


def stats(values: list[float]) -> dict:
    return {
        "n": len(values),
        "p50": rnd(pct(values, 50), 2),
        "p95": rnd(pct(values, 95), 2),
        "max": rnd(max(values), 2) if values else None,
    }


def peak_rate(times: list[float], window: float = 10) -> float:
    """Highest number of events in any `window` seconds, per second."""
    times = sorted(times)
    best = j = 0
    for i, t in enumerate(times):
        while times[j] < t - window:
            j += 1
        best = max(best, i - j + 1)
    return best / window


class Run:
    def __init__(self, path: str) -> None:
        self.path = path
        self.name = os.path.basename(path.rstrip("/"))
        with open(os.path.join(path, "meta.json")) as f:
            self.meta = json.load(f)
        self.marks = list(jsonl(os.path.join(path, "marks.jsonl")))
        self.weights = {
            f"gw{k}": w for k, w in enumerate(self.meta["weights"], start=1)
        }
        self.events = list(jsonl(os.path.join(path, "nodes", "events.jsonl")))
        self.snaps = list(jsonl(os.path.join(path, "nodes", "nodes.jsonl")))
        try:
            with open(os.path.join(path, "nodes", "keys.json")) as f:
                self.keys = json.load(f)
        except FileNotFoundError:
            self.keys = {}
        self.samples = {}  # host role -> samples
        for d in sorted(glob.glob(os.path.join(path, "*", "samples.jsonl"))):
            self.samples[os.path.basename(os.path.dirname(d))] = list(jsonl(d))

    def mark(self, name: str, gw: str | None = None) -> float | None:
        for m in self.marks:
            if m["mark"] == name and (gw is None or m.get("gw") == gw):
                return m["t"]
        return None

    # --- distribution ------------------------------------------------------------------------

    def deviation(self, by_gw: dict, online: set[str]) -> float | None:
        total = sum(by_gw.get(g, 0) for g in online)
        wsum = sum(self.weights[g] for g in online)
        if not total or not wsum:
            return None
        return max(
            abs(by_gw.get(g, 0) / total - self.weights[g] / wsum)
            / (self.weights[g] / wsum)
            for g in online
        )

    def distribution(self) -> dict:
        all_gw = set(self.weights)
        reboot = self.mark("reboot")
        back = self.mark("gw_back")
        end = self.mark("end") or (self.snaps[-1]["t"] if self.snaps else 0)
        series = []
        for s in self.snaps:
            online = all_gw
            if reboot and s["t"] >= reboot and (not back or s["t"] < back):
                online = all_gw - {self.marks_gw("reboot")}
            series.append((s["t"], self.deviation(s["by_gw"], online)))
        res = {}
        if self.snaps:
            last = self.snaps[-1]
            res["final_by_gw"] = last["by_gw"]
            res["final_up"] = last["up"]
            res["final_max_dev"] = rnd(self.deviation(last["by_gw"], all_gw), 3)
        if back:
            # Converged = within 10% (20%) of the weighted target from then on until the end
            for key, limit in (("converge_s", 0.10), ("converge_20_s", 0.20)):
                conv = None
                for t, dev in series:
                    if t < back:
                        continue
                    if dev is not None and dev <= limit:
                        if conv is None:
                            conv = t
                    else:
                        conv = None
                res[key] = rnd(conv - back) if conv else None
            res["observed_s"] = rnd(end - back)
            res["dev_at_back"] = rnd(
                next((d for t, d in series if t >= back and d is not None), None), 3
            )
            back_gw = self.marks_gw("gw_back")
            share = [
                (t, s["by_gw"].get(back_gw, 0) / max(1, s["up"]))
                for (t, _), s in zip(series, self.snaps, strict=True)
                if t >= back
            ]
            for mins in (5, 15, 30, 60):
                v = next((x for t, x in share if t >= back + mins * 60), None)
                res[f"rebooted_gw_share_{mins}m"] = rnd(v, 3)
        return res

    def marks_gw(self, name: str) -> str | None:
        return next((m.get("gw") for m in self.marks if m["mark"] == name), None)

    # --- nodes -------------------------------------------------------------------------------

    def bringup(self) -> dict:
        start = self.mark("nodes_start")
        n = self.meta["nodes"]
        res = {}
        if not start:
            return res
        for frac in (0.5, 0.9, 0.99, 1.0):
            t = next((s["t"] for s in self.snaps if s["up"] >= n * frac), None)
            res[f"t{int(frac * 100)}_s"] = rnd(t - start) if t else None
        return res

    def evicted_keys(self) -> dict[str, float]:
        out = {}
        for log in glob.glob(os.path.join(self.path, "gw*", "logs", "worker-gw*.log")):
            for t, line in log_lines(log):
                m = EVICT.search(line)
                if m:
                    out.setdefault(m[1], t)
        return out

    def reconnects(self) -> dict:
        reboot = self.mark("reboot")
        rgw = self.marks_gw("reboot")
        warm = self.mark("warm") or 0
        evicted = self.evicted_keys()
        evicted_nodes = {self.keys[k]: t for k, t in evicted.items() if k in self.keys}
        recs = [e for e in self.events if e.get("ev") == "reconnect"]
        api_ms = [e["api_ms"] for e in recs if e.get("api_ms") is not None]
        res = {
            "total": len(recs),
            "failed": sum(1 for e in recs if e["rc"] != 0),
            "api_v1": sum(1 for e in recs if e.get("api") == "v1"),
            "api_ms": stats(api_ms),
            "after_warm": sum(1 for e in recs if e["t0"] >= warm),
            "after_warm_nodes": len({e["node"] for e in recs if e["t0"] >= warm}),
            "evictions": len(evicted),
        }
        # Outages: from the node's last successful check before a failed check while it was up,
        # until its next successful reconnect (checkuplink only notices once a minute).
        open_outage: dict[int, dict] = {}
        outages = {"reboot": [], "rebalance": [], "other": []}
        attempts = []
        for e in sorted(self.events, key=lambda e: e["t"]):
            if e.get("ev") == "check_fail" and e.get("was_up"):
                cause = "other"
                if reboot and e["gw"] == rgw and e["t"] >= reboot:
                    cause = "reboot"
                elif e["node"] in evicted_nodes and e["t"] >= evicted_nodes[e["node"]]:
                    cause = "rebalance"
                start = e.get("last_ok") or e["t"]
                if cause == "reboot":
                    start = max(start, reboot)
                open_outage[e["node"]] = {"start": start, "cause": cause, "n": 0}
            elif e.get("ev") == "reconnect" and e["node"] in open_outage:
                o = open_outage[e["node"]]
                o["n"] += 1
                if e["rc"] == 0:
                    outages[o["cause"]].append(e["t0"] + e["dur"] - o["start"])
                    attempts.append(o["n"])
                    del open_outage[e["node"]]
        res["outage_s"] = {c: stats(v) for c, v in outages.items()}
        res["outage_unresolved"] = len(open_outage)
        res["attempts_per_outage"] = stats(attempts)
        res["gw_changes_after_warm"] = sum(
            1
            for e in recs
            if e["t0"] >= warm and e.get("prev_gw") and e.get("gw") != e.get("prev_gw")
        )
        return res

    # --- keys / queue ------------------------------------------------------------------------

    def keys_flow(self) -> dict:
        pubs: dict[str, list[float]] = {}
        topics: dict[str, int] = {}
        removes = 0
        mqtt_msgs = mqtt_bytes = 0
        for m in jsonl(os.path.join(self.path, "ctl", "mqtt.jsonl")):
            mqtt_msgs += 1
            mqtt_bytes += m["len"]
            parts = m["topic"].split("/")
            if parts[0] == "wireguard" and len(parts) == 3 and "payload" in m:
                pubs.setdefault(m["payload"], []).append(m["t"])
                target = "all" if parts[2] == "all" else "worker"
                topics[target] = topics.get(target, 0) + 1
            elif m["topic"].endswith("/remove"):
                removes += 1
        res = {
            "mqtt_msgs": mqtt_msgs,
            "mqtt_bytes": mqtt_bytes,
            "key_publishes": topics,
            "remove_msgs": removes,
            "key_publish_peak_per_s": rnd(
                peak_rate([t for ts in pubs.values() for t in ts]), 1
            ),
            "per_gw": {},
        }
        lags = []
        for log in sorted(
            glob.glob(os.path.join(self.path, "gw*", "logs", "worker-gw*.log"))
        ):
            gw = os.path.basename(log)[len("worker-") : -len(".log")]
            times = []
            removed = 0
            for t, line in log_lines(log):
                m = PROCESS.search(line)
                if m:
                    times.append(t)
                    ts = [p for p in pubs.get(m[1], []) if p <= t + 0.5]
                    if ts:
                        lags.append(t - ts[-1])
                elif REMOVE.search(line):
                    removed += 1
            res["per_gw"][gw] = {
                "processed": len(times),
                "peak_per_s": rnd(peak_rate(times), 1),
                "removed": removed,
            }
        res["lag_s"] = stats(lags)
        return res

    # --- resources ---------------------------------------------------------------------------

    def window(self) -> tuple[float, float]:
        """Steady window for resource/BUM averages: after warm-up until the first action."""
        warm = self.mark("warm") or 0
        end = (
            self.mark("drain")
            or self.mark("reboot")
            or self.mark("end")
            or float("inf")
        )
        return warm + 60, end

    def cpu(self) -> dict:
        lo, hi = self.window()
        res = {"proc_pct": {}, "host": {}}
        for host, samples in self.samples.items():
            procs: dict[str, list[float]] = {}
            hostc: dict[str, list[float]] = {"busy": [], "softirq": []}
            for a, b in zip(samples, samples[1:]):
                dt = b["t"] - a["t"]
                if dt <= 0:
                    continue
                for role, pb in b["proc"].items():
                    pa = a["proc"].get(role)
                    if not pa or not pb:
                        continue
                    ticks = sum(pb[k] - pa[k] for k in ("utime", "stime"))
                    procs.setdefault(role, []).append(
                        (b["t"], ticks / b["clk_tck"] / dt * 100)
                    )
                ha, hb = a["host"], b["host"]
                tot = sum(hb[k] - ha[k] for k in hb if k != "mem_available_kb")
                if tot > 0 and lo <= b["t"] <= hi:
                    idle = hb["idle"] + hb["iowait"] - ha["idle"] - ha["iowait"]
                    hostc["busy"].append((tot - idle) / tot * 100)
                    hostc["softirq"].append((hb["softirq"] - ha["softirq"]) / tot * 100)
            for role, vals in procs.items():
                steady = [v for t, v in vals if lo <= t <= hi]
                res["proc_pct"][role] = {
                    "steady_avg": rnd(statistics.fmean(steady)) if steady else None,
                    "max": rnd(max(v for _, v in vals)),
                }
            res["host"][host] = {
                k: rnd(statistics.fmean(v)) if v else None for k, v in hostc.items()
            }
        return res

    def bum(self) -> dict:
        """Per gateway averages in the steady window: replication of the BUM test traffic."""
        lo, hi = self.window()
        res = {}
        for host, samples in self.samples.items():
            if not host.startswith("gw"):
                continue
            inwin = [s for s in samples if lo <= s["t"] <= hi]
            if len(inwin) < 2:
                continue
            a, b = inwin[0], inwin[-1]
            dt = b["t"] - a["t"]
            for gw, gb in b["gw"].items():
                ga = a["gw"].get(gw)
                if (
                    not ga
                    or "wg-welt" not in gb["links"]
                    or "wg-welt" not in ga["links"]
                ):
                    continue

                def rate(link, d, key):
                    try:
                        return (
                            gb["links"][link][d][key] - ga["links"][link][d][key]
                        ) / dt
                    except KeyError:
                        return None

                drops = (rate("wg-welt", "tx", "errors") or 0) + (
                    rate("wg-welt", "tx", "dropped") or 0
                )
                res[gw] = {
                    "peers": gb["peers"],
                    "connected": gb["hs180"],
                    "fdb": gb["fdb"],
                    "vx_tx_pps": rnd(rate("vx-welt", "tx", "packets"), 2),
                    "wg_tx_pps": rnd(rate("wg-welt", "tx", "packets"), 1),
                    "wg_tx_drop_pps": rnd(drops, 1),
                    "wan_tx_kbps": rnd((rate("wan", "tx", "bytes") or 0) * 8 / 1000, 1),
                    "wan_rx_kbps": rnd((rate("wan", "rx", "bytes") or 0) * 8 / 1000, 1),
                }
        return res

    # --- output ------------------------------------------------------------------------------

    def broker_requests(self) -> dict | None:
        """wgkex_broker_key_exchange_requests_total by result, from the end of the run."""
        try:
            f = open(os.path.join(self.path, "broker-metrics.prom"))
        except FileNotFoundError:
            return None
        res: dict[str, float] = {}
        with f:
            for line in f:
                m = re.match(
                    r"wgkex_broker_key_exchange_requests_total\{(.*)\} (\S+)", line
                )
                if m:
                    result = re.search(r'result="([^"]*)"', m[1])
                    key = result[1] if result else "?"
                    res[key] = res.get(key, 0) + float(m[2])
        return res

    def warnings(self) -> list[str]:
        """Things that make a run unrepresentative of wgkex itself."""
        res: list[str] = []
        online = self.mark("workers_online") or 0
        no_worker = sum(
            1
            for t, line in log_lines(
                os.path.join(self.path, "ctl", "logs", "broker.log")
            )
            if t >= online and "No worker online" in line
        )
        if no_worker:
            res.append(f"broker answered 'No worker online' {no_worker}x")
        v1 = sum(
            1
            for e in self.events
            if e.get("ev") == "reconnect" and e.get("api") == "v1"
        )
        if v1:
            res.append(f"{v1} v1 fallbacks")
        sim = sum(1 for e in self.events if e.get("ev") == "sim_error")
        if sim:
            res.append(f"{sim} node simulator errors")
        return res

    def summary(self) -> dict:
        return {
            "warnings": self.warnings(),
            "broker_requests": self.broker_requests(),
            "run": self.name,
            "label": self.meta.get("label"),
            "commit": self.meta.get("commit"),
            "scenario": self.meta.get("scenario"),
            "rebalancing": self.meta.get("rebalancing"),
            "nodes": self.meta.get("nodes"),
            "weights": self.weights,
            "overlay": self.meta.get("overlay"),
            "error": next(
                (m.get("error") for m in self.marks if m["mark"] == "error"), None
            ),
            "aborted": any(m["mark"] == "aborted" for m in self.marks),
            "marks": {m["mark"]: m for m in self.marks},
            "bringup": self.bringup(),
            "distribution": self.distribution(),
            "reconnects": self.reconnects(),
            "keys": self.keys_flow(),
            "cpu": self.cpu(),
            "bum": self.bum(),
        }

    def plot(self) -> None:
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            return
        if not self.snaps:
            return
        t0 = self.mark("nodes_start") or self.snaps[0]["t"]
        fig, axes = plt.subplots(3, 1, figsize=(11, 10), sharex=True)
        ax = axes[0]
        for gw in sorted(self.weights):
            ax.plot(
                [(s["t"] - t0) / 60 for s in self.snaps],
                [s["by_gw"].get(gw, 0) for s in self.snaps],
                label=f"{gw} (w={self.weights[gw]})",
            )
        ax.plot(
            [(s["t"] - t0) / 60 for s in self.snaps],
            [s["up"] for s in self.snaps],
            "k--",
            label="up",
        )
        ax.set_ylabel("nodes up per gateway (node view)")
        ax.legend(loc="upper left", fontsize=8)
        ax = axes[1]
        for host, samples in self.samples.items():
            if not host.startswith("gw"):
                continue
            for gw in sorted({g for s in samples for g in s["gw"]}):
                ax.plot(
                    [(s["t"] - t0) / 60 for s in samples if gw in s["gw"]],
                    [s["gw"][gw]["peers"] for s in samples if gw in s["gw"]],
                    label=f"{gw} peers",
                )
                ax.plot(
                    [(s["t"] - t0) / 60 for s in samples if gw in s["gw"]],
                    [s["gw"][gw]["hs180"] for s in samples if gw in s["gw"]],
                    ":",
                    label=f"{gw} handshake<180s",
                )
        ax.set_ylabel("gateway WireGuard peers")
        ax.legend(loc="upper left", fontsize=7, ncol=2)
        ax = axes[2]
        recs = [e for e in self.events if e.get("ev") == "reconnect"]
        if recs:
            ax.hist(
                [(e["t0"] - t0) / 60 for e in recs],
                bins=max(10, int((self.snaps[-1]["t"] - t0) / 30)),
                color="tab:red",
            )
        ax.set_ylabel("reconnects / 30 s")
        ax.set_xlabel("minutes since node start")
        for ax in axes:
            for m in self.marks:
                if m["mark"] in ("warm", "drain", "reboot", "gw_back", "end"):
                    ax.axvline((m["t"] - t0) / 60, color="grey", lw=0.8, ls="--")
        axes[0].set_title(self.name)
        fig.tight_layout()
        fig.savefig(os.path.join(self.path, "timeline.png"), dpi=110)
        plt.close(fig)


def table(rows: list[dict]) -> str:
    def g(d, *path):
        for p in path:
            if not isinstance(d, dict):
                return None
            d = d.get(p)
        return d

    def fmt(v):
        return "–" if v is None else str(v)

    cols = [
        ("run", lambda r: r["run"]),
        ("t99 up s", lambda r: g(r, "bringup", "t99_s")),
        ("keys/s peak (pub)", lambda r: g(r, "keys", "key_publish_peak_per_s")),
        (
            "processed/s peak per gw",
            lambda r: max(
                (v["peak_per_s"] for v in g(r, "keys", "per_gw").values()), default=None
            ),
        ),
        ("lag p95 ms", lambda r: _ms(g(r, "keys", "lag_s", "p95"))),
        ("final max dev", lambda r: g(r, "distribution", "final_max_dev")),
        ("converge ≤10% s", lambda r: _conv(g(r, "distribution"), "converge_s")),
        ("converge ≤20% s", lambda r: _conv(g(r, "distribution"), "converge_20_s")),
        ("gw1 share 30m", lambda r: g(r, "distribution", "rebooted_gw_share_30m")),
        ("reconnects*", lambda r: g(r, "reconnects", "after_warm")),
        ("nodes*", lambda r: g(r, "reconnects", "after_warm_nodes")),
        ("evictions", lambda r: g(r, "reconnects", "evictions")),
        (
            "outage p50/p95 s (reboot)",
            lambda r: _pp(g(r, "reconnects", "outage_s", "reboot")),
        ),
        (
            "outage p50/p95 s (rebal.)",
            lambda r: _pp(g(r, "reconnects", "outage_s", "rebalance")),
        ),
        ("api p95 ms", lambda r: g(r, "reconnects", "api_ms", "p95")),
        ("v1 fallbacks", lambda r: g(r, "reconnects", "api_v1")),
        (
            "rate limited",
            lambda r: (
                None
                if r.get("broker_requests") is None
                else int(r["broker_requests"].get("rate_limited", 0))
            ),
        ),
        ("peers/gw", lambda r: _avg([v["peers"] for v in g(r, "bum").values()])),
        ("fdb/gw", lambda r: _avg([v["fdb"] for v in g(r, "bum").values()])),
        (
            "wg tx pps/gw",
            lambda r: _avg([v["wg_tx_pps"] for v in g(r, "bum").values()]),
        ),
        (
            "wg tx drop pps/gw",
            lambda r: _avg([v["wg_tx_drop_pps"] for v in g(r, "bum").values()]),
        ),
        (
            "worker CPU %",
            lambda r: _avg(
                [
                    v["steady_avg"]
                    for k, v in g(r, "cpu", "proc_pct").items()
                    if k.startswith("worker-")
                ]
            ),
        ),
        ("broker CPU %", lambda r: g(r, "cpu", "proc_pct", "broker", "steady_avg")),
        ("warnings", lambda r: "; ".join(r.get("warnings") or []) or None),
    ]
    lines = [
        "| " + " | ".join(c for c, _ in cols) + " |",
        "|" + "---|" * len(cols),
    ]
    for r in rows:
        vals = []
        for _, f in cols:
            try:
                vals.append(fmt(f(r)))
            except (TypeError, AttributeError, KeyError):
                vals.append("–")
        lines.append("| " + " | ".join(vals) + " |")
    lines.append("")
    lines.append(
        "\\* after warm-up (bring-up excluded). Outage = last good check → reconnected "
        "(includes checkuplink's 1 min check interval). BUM columns: steady window before "
        "any reboot/drain. n/c = not converged within the observation window. Runs with "
        "warnings are not representative."
    )
    return "\n".join(lines)


def _ms(v):
    return None if v is None else max(0, round(v * 1000))


def _conv(dist, key):
    if not dist or "observed_s" not in dist:
        return None
    if dist.get(key) is not None:
        return dist[key]
    return f"n/c in {dist['observed_s']} (final dev {dist.get('final_max_dev')})"


def _pp(s):
    if not s or not s.get("n"):
        return None
    return f"{s['p50']}/{s['p95']} (n={s['n']})"


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return rnd(statistics.fmean(vals)) if vals else None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("results")
    p.add_argument("--no-plots", action="store_true")
    args = p.parse_args()
    rows = []
    for meta in sorted(glob.glob(os.path.join(args.results, "*", "meta.json"))):
        run = Run(os.path.dirname(meta))
        try:
            rows.append(run.summary())
            if not args.no_plots:
                run.plot()
        except Exception as e:
            print(f"{run.name}: analysis failed: {e!r}", file=sys.stderr)
            rows.append({"run": run.name, "analysis_error": repr(e)})
    with open(os.path.join(args.results, "summary.json"), "w") as f:
        json.dump({"runs": rows}, f, indent=2)
    md = table(rows)
    with open(os.path.join(args.results, "summary.md"), "w") as f:
        f.write(md + "\n")
    print(md)


if __name__ == "__main__":
    main()
