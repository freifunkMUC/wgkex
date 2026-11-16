"""Tests for the worker metrics listener."""

import socket
import unittest
import urllib.request

from wgkex.worker import observability


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ObservabilityTest(unittest.TestCase):
    def test_disabled_without_port(self):
        self.assertIsNone(observability.start_http_server(None, "127.0.0.1"))
        self.assertIsNone(observability.start_http_server(0, "127.0.0.1"))

    def test_serves_metrics(self):
        port = _free_port()
        server = observability.start_http_server(port, "127.0.0.1")
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        observability.EVICTIONS.labels("drain").inc()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as r:
            text = r.read().decode()
        self.assertIn("wgkex_worker_queue_depth", text)
        self.assertIn('wgkex_worker_evictions_total{kind="drain"}', text)
        self.assertIn("wgkex_worker_netlink_operation_seconds", text)


if __name__ == "__main__":
    unittest.main()
