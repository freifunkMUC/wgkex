"""Per-host operations of the e2e rig, usable in-process or remotely through a tiny HTTP agent.

The controller (scenario.py) drives every host of the rig through the same interface:
``Ops`` when the role runs on the controller host itself, ``Remote`` for other hosts, which run
``python3 rigops.py agent``. The agent only listens on the rig's private network and only accepts
requests from the controller address given on its command line; it is a test tool, not a service.
"""

import argparse
import base64
import http.server
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
NET = os.path.join(HERE, "lib", "net.sh")
AGENT_PORT = 7700


# Only the broker subscribes to this topic, so it shows up in the mosquitto subscribe log
BROKER_ONLY_TOPIC = "wireguard-worker/+/+/data"


class Ops:
    def __init__(self, base: str, python: str = sys.executable) -> None:
        self.base = os.path.abspath(base)
        self.python = python
        self.procs: dict[str, subprocess.Popen] = {}
        self.out = self.base
        self.env = dict(os.environ)
        self._broker_subs_before = 0

    # --- lifecycle ---------------------------------------------------------------------------

    def ping(self) -> dict:
        return {"host": os.uname().nodename, "time": time.time()}

    def prepare(self, run: str, role: str, env: dict, src: str | None = None) -> str:
        """Starts a new run: output directory <base>/<run>/<role>, environment for net.sh."""
        self.shutdown(keep_network=True)
        self.out = os.path.join(self.base, run, role)
        os.makedirs(os.path.join(self.out, "logs"), exist_ok=True)
        self.env = dict(os.environ, **env)
        self.src = os.path.abspath(src) if src else None
        return self.out

    def net(self, *argv: str) -> None:
        subprocess.run(["bash", NET, *argv], check=True, env=self.env)

    def write_file(self, path: str, content: str) -> None:
        with open(path, "w") as f:
            f.write(content)

    # --- processes ---------------------------------------------------------------------------

    def start(
        self,
        role: str,
        cmd: list[str],
        env: dict | None = None,
        cwd: str | None = None,
    ) -> int:
        self.stop(role, signal.SIGKILL)
        logf = open(os.path.join(self.out, "logs", f"{role}.log"), "ab")
        penv = dict(self.env, TZ="UTC", PYTHONUNBUFFERED="1", **(env or {}))
        self.procs[role] = subprocess.Popen(
            cmd, stdout=logf, stderr=subprocess.STDOUT, env=penv, cwd=cwd
        )
        logf.close()
        self._write_pids()
        return self.procs[role].pid

    def stop(self, role: str, sig: int = signal.SIGTERM, timeout: float = 20) -> None:
        proc = self.procs.pop(role, None)
        if proc is None:
            return
        if proc.poll() is None:
            proc.send_signal(sig)
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        self._write_pids()

    def alive(self) -> dict:
        return {r: p.poll() for r, p in self.procs.items()}

    def _write_pids(self) -> None:
        tmp = os.path.join(self.out, "pids.json.tmp")
        with open(tmp, "w") as f:
            json.dump({r: p.pid for r, p in self.procs.items() if p.poll() is None}, f)
        os.replace(tmp, os.path.join(self.out, "pids.json"))

    def _in_gw(self, k: int, argv: list[str]) -> list[str]:
        # Own UTS namespace, so socket.gethostname() (the worker name) is gw<k>
        return [
            "ip",
            "netns",
            "exec",
            f"e2egw{k}",
            "unshare",
            "--uts",
            "bash",
            "-c",
            f'hostname gw{k} && exec "$@"',
            "_",
            *argv,
        ]

    # --- roles -------------------------------------------------------------------------------

    def start_mosquitto(self) -> None:
        conf = os.path.join(self.out, "mosquitto.conf")
        self.write_file(
            conf,
            "listener 1883 0.0.0.0\n"
            "allow_anonymous true\n"
            "persistence false\n"
            "max_queued_messages 100000\n"
            "log_dest stdout\n"
            "log_type error\nlog_type warning\nlog_type notice\nlog_type information\n"
            "log_type subscribe\n"
            "log_timestamp_format %Y-%m-%dT%H:%M:%S\n",
        )
        self.start("mosquitto", ["mosquitto", "-c", conf])

    def _broker_subscriptions(self) -> int:
        try:
            with open(
                os.path.join(self.out, "logs", "mosquitto.log"), errors="replace"
            ) as f:
                return sum(1 for line in f if line.rstrip().endswith(BROKER_ONLY_TOPIC))
        except FileNotFoundError:
            return 0

    def broker_subscribed(self, timeout: float = 20) -> bool:
        """True once the broker started by the last start_broker() subscribed (see the README)."""
        end = time.time() + timeout
        while time.time() < end:
            if self._broker_subscriptions() > self._broker_subs_before:
                return True
            time.sleep(0.5)
        return False

    def start_broker(self, config: str) -> None:
        self._broker_subs_before = self._broker_subscriptions()
        path = os.path.join(self.out, "config-broker.yaml")
        self.write_file(path, config)
        # The logger always reads the default config location
        self.write_file("/etc/wgkex.yaml", config)
        self.start(
            "broker",
            [self.python, "-u", "-m", "wgkex.broker.app"],
            env={"WGKEX_CONFIG_FILE": path, "PYTHONPATH": self.src},
            cwd=self.src,
        )

    def start_gateway(self, k: int, config: str, bum_pps: float) -> None:
        path = os.path.join(self.out, f"config-gw{k}.yaml")
        self.write_file(path, config)
        self.write_file("/etc/wgkex.yaml", config)
        self.start(
            f"http-gw{k}", self._in_gw(k, [self.python, f"{HERE}/lib/gw_http.py"])
        )
        if bum_pps > 0:
            self.start(
                f"bum-gw{k}",
                self._in_gw(
                    k, [self.python, f"{HERE}/lib/bum.py", "vx-welt", str(bum_pps)]
                ),
            )
        self.start(
            f"worker-gw{k}",
            self._in_gw(
                k,
                [self.python, "-u", "-c", "from wgkex.worker.app import main; main()"],
            ),
            env={"WGKEX_CONFIG_FILE": path, "PYTHONPATH": self.src},
            cwd=self.src,
        )

    def stop_gateway(self, k: int, sig: int = signal.SIGTERM) -> None:
        for role in (f"worker-gw{k}", f"http-gw{k}", f"bum-gw{k}"):
            self.stop(role, sig)

    def signal_worker(self, k: int, sig: int) -> None:
        """Signals the worker python process (the child of ip/unshare/bash exec chain)."""
        proc = self.procs.get(f"worker-gw{k}")
        if proc is not None and proc.poll() is None:
            os.kill(proc.pid, sig)

    def start_collector(self, gws: list[int], mqtt_host: str = "") -> None:
        self.start(
            "collector",
            [
                self.python,
                f"{HERE}/collector.py",
                "--out",
                self.out,
                "--gw",
                ",".join(str(k) for k in gws),
                "--mqtt-host",
                mqtt_host,
                "--pids",
                os.path.join(self.out, "pids.json"),
            ],
        )

    def start_nodesim(self, nodes: int, jitter: str, dump: bool = False) -> None:
        cmd = [
            self.python,
            f"{HERE}/nodesim.py",
            "--nodes",
            str(nodes),
            "--out",
            self.out,
            "--jitter",
            jitter,
        ]
        if dump:
            cmd.append("--dump-assignments")
        self.start("nodesim", cmd)

    def last_snapshot(self) -> dict:
        try:
            with open(os.path.join(self.out, "nodes.jsonl"), "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 262144))
                lines = f.read().splitlines()
            snap = json.loads(lines[-1])
            snap.pop("assign", None)
            return snap
        except (FileNotFoundError, IndexError, json.JSONDecodeError):
            return {}

    def shutdown(self, keep_network: bool = False) -> None:
        self.stop("nodesim", timeout=60)
        for role in [r for r in self.procs if r.startswith("worker-")]:
            self.stop(role)
        for role in list(self.procs):
            if role not in ("collector", "broker", "mosquitto"):
                self.stop(role)
        for role in ("collector", "broker", "mosquitto"):
            self.stop(role)
        if not keep_network:
            self.net("teardown")

    def results(self, run: str) -> str:
        """Returns the run's output of this host as base64 encoded tar.gz."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            path = os.path.join(self.base, run)
            if os.path.isdir(path):
                for name in os.listdir(path):
                    tar.add(os.path.join(path, name), arcname=name)
        return base64.b64encode(buf.getvalue()).decode()


class Remote:
    """Calls Ops methods on an agent."""

    def __init__(self, host: str) -> None:
        # host or host:port (several agents on one host are only useful for testing)
        if ":" not in host:
            host = f"{host}:{AGENT_PORT}"
        self.url = f"http://{host}/"

    def call(self, method: str, *args, timeout: float = 1800, **kwargs):
        body = json.dumps({"method": method, "args": args, "kwargs": kwargs}).encode()
        req = urllib.request.Request(
            self.url, body, {"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            res = json.load(resp)
        if "error" in res:
            raise RuntimeError(f"{self.url} {method}: {res['error']}")
        return res["result"]

    def __getattr__(self, method: str):
        return lambda *a, **kw: self.call(method, *a, **kw)


class Local:
    def __init__(self, ops: Ops) -> None:
        self.ops = ops

    def __getattr__(self, method: str):
        return getattr(self.ops, method)


def serve(ops: Ops, listen: str, port: int, allow: set[str]) -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            if self.client_address[0] not in allow:
                self.send_error(403)
                return
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = req["method"]
            try:
                if method.startswith("_") or not callable(getattr(ops, method, None)):
                    raise AttributeError(method)
                res = {"result": getattr(ops, method)(*req["args"], **req["kwargs"])}
            except Exception as e:  # reported to the controller
                res = {"error": repr(e)}
            body = json.dumps(res).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            print(time.strftime("%H:%M:%S"), fmt % args, flush=True)

    # Requests are handled one at a time on purpose: operations must not interleave
    server = http.server.HTTPServer((listen, port), Handler)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        server.serve_forever()
    finally:
        ops.shutdown()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["agent"])
    p.add_argument("--base", required=True, help="results directory on this host")
    p.add_argument("--listen", default="0.0.0.0")
    p.add_argument("--port", type=int, default=AGENT_PORT)
    p.add_argument("--allow", required=True, help="controller IP(s), comma separated")
    args = p.parse_args()
    serve(Ops(args.base), args.listen, args.port, set(args.allow.split(",")))


if __name__ == "__main__":
    main()
