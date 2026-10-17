"""Unit tests for rebalance.py"""

import json
import random
import threading
import unittest
from unittest import mock

from wgkex.config.config import Rebalancing
from wgkex.worker import observability, rebalance

_DOMAINS = ["ffmuc_a", "ffmuc_b"]


def _evictions(kind: str) -> float:
    value = observability.REGISTRY.get_sample_value(
        "wgkex_worker_evictions_total", {"kind": kind}
    )
    return value or 0.0


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _iface(domain: str) -> str:
    return "wg-" + domain.removeprefix("ffmuc_")


class RebalancerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.tracker = rebalance.PeerAddTracker(clock=self.clock)
        self.peers = {"wg-a": [f"a{i}" for i in range(100)], "wg-b": ["b0", "b1"]}
        self.evicted = []
        self.cfg = Rebalancing(
            enabled=True, interval=60, max_evictions_ratio=0.02, eviction_cooldown=3600
        )

    def rebalancer(self, **kwargs) -> rebalance.Rebalancer:
        def evict(iface, key):
            self.evicted.append((iface, key))
            self.peers[iface].remove(key)

        args = {
            "cfg": self.cfg,
            "domains": _DOMAINS,
            "wg_interface_name": _iface,
            "tracker": self.tracker,
            "evict": evict,
            "connected_peers": lambda iface: list(self.peers[iface]),
            "clock": self.clock,
            "rng": random.Random(0),
            "protected": lambda key: False,
        }
        args.update(kwargs)
        return rebalance.Rebalancer(**args)

    @staticmethod
    def hint(excess: int, domains=None) -> str:
        return json.dumps(
            {"excess": excess, "domains": domains if domains is not None else _DOMAINS}
        )

    def test_protected_peers_not_evicted(self):
        """Allowlisted peers are never evicted, even when draining."""
        protected = {f"a{i}" for i in range(95)}
        r = self.rebalancer(protected=lambda key: key in protected)
        r.handle_instruction(
            json.dumps({"excess": 100, "domains": _DOMAINS, "max_evictions_ratio": 1.0})
        )
        r.run_once()
        self.assertTrue(self.evicted)
        self.assertFalse(protected & {key for _, key in self.evicted})

    def test_no_hint_no_eviction(self):
        """Nothing is evicted without a hint from the broker."""
        self.assertListEqual(self.rebalancer().run_once(), [])

    def test_eviction_is_rate_limited(self):
        """At most max_evictions_ratio of the connected peers are evicted per round."""
        before = _evictions("rebalance")
        r = self.rebalancer()
        r.handle_instruction(self.hint(50))
        # 102 connected peers * 0.02 = 2
        self.assertEqual(len(r.run_once()), 2)
        self.assertEqual(_evictions("rebalance"), before + 2)

    def test_drain_hint_uses_its_rate(self):
        """Hints for draining workers carry a faster eviction rate, which is clamped to 1."""
        drain_before = _evictions("drain")
        r = self.rebalancer()
        r.handle_instruction(
            json.dumps({"excess": 102, "domains": _DOMAINS, "max_evictions_ratio": 0.1})
        )
        # 102 connected peers * 0.1 = 10
        self.assertEqual(len(r.run_once()), 10)
        r.handle_instruction(
            json.dumps({"excess": 500, "domains": _DOMAINS, "max_evictions_ratio": 5})
        )
        self.assertEqual(len(r.run_once()), 92)
        self.assertEqual(_evictions("drain"), drain_before + 102)

    def test_invalid_drain_rate_ignored(self):
        r = self.rebalancer()
        r.handle_instruction(
            json.dumps({"excess": 5, "domains": _DOMAINS, "max_evictions_ratio": "x"})
        )
        self.assertListEqual(r.run_once(), [])

    def test_eviction_at_least_one(self):
        """At least one peer is evicted per round on small workers."""
        self.peers = {"wg-a": ["a0", "a1"], "wg-b": []}
        r = self.rebalancer()
        r.handle_instruction(self.hint(5))
        self.assertEqual(len(r.run_once()), 1)

    def test_eviction_limited_by_excess(self):
        """Never more peers than the excess are evicted for one hint, also over multiple rounds."""
        self.cfg.max_evictions_ratio = 0.5
        r = self.rebalancer()
        r.handle_instruction(self.hint(3))
        self.assertEqual(len(r.run_once()), 3)
        self.assertListEqual(r.run_once(), [])

    def test_stale_hint_is_ignored(self):
        """Hints older than 2.5 intervals are ignored (broker stopped sending them)."""
        r = self.rebalancer()
        r.handle_instruction(self.hint(50))
        self.clock.now += 151
        self.assertListEqual(r.run_once(), [])

    def test_only_allowed_domains(self):
        """Peers are only evicted from domains named in the hint."""
        self.cfg.max_evictions_ratio = 1
        r = self.rebalancer()
        r.handle_instruction(self.hint(50, domains=["ffmuc_b"]))
        evicted = r.run_once()
        self.assertListEqual(sorted(evicted), ["b0", "b1"])
        self.assertTrue(all(iface == "wg-b" for iface, _ in self.evicted))

    def test_unknown_domains_ignored(self):
        """Domains not served by this worker are ignored."""
        r = self.rebalancer()
        r.handle_instruction(self.hint(50, domains=["ffmuc_unknown"]))
        self.assertListEqual(r.run_once(), [])

    def test_most_recently_added_evicted_first(self):
        """The most recently (re-)added peers are evicted first."""
        for key in ("a5", "a7", "a9"):
            self.tracker.note_added("wg-a", key)
            self.clock.now += 1
        r = self.rebalancer()
        r.handle_instruction(self.hint(50))
        self.assertListEqual(r.run_once(), ["a9", "a7"])

    def test_cooldown_prevents_ping_pong(self):
        """A peer is not evicted again within the cooldown, even if it reconnects."""
        self.peers = {"wg-a": ["a0"], "wg-b": []}
        r = self.rebalancer()
        r.handle_instruction(self.hint(5))
        self.assertListEqual(r.run_once(), ["a0"])
        self.peers["wg-a"].append("a0")
        self.clock.now += 60
        r.handle_instruction(self.hint(5))
        self.assertListEqual(r.run_once(), [])
        self.clock.now += 3600
        r.handle_instruction(self.hint(5))
        self.assertListEqual(r.run_once(), ["a0"])

    def test_invalid_hints_ignored(self):
        """Malformed hints don't raise and don't trigger evictions."""
        r = self.rebalancer()
        for payload in (b"garbage", b"{}", b'{"excess": "x", "domains": []}', b"[]"):
            r.handle_instruction(payload)
        self.assertListEqual(r.run_once(), [])

    def test_failed_eviction_continues(self):
        """A failing eviction is logged and doesn't stop the round."""
        self.cfg.max_evictions_ratio = 1
        self.peers = {"wg-a": ["a0", "a1"], "wg-b": []}
        calls = []

        def evict(iface, key):
            calls.append(key)
            if len(calls) == 1:
                raise RuntimeError("netlink error")

        r = self.rebalancer(evict=evict)
        r.handle_instruction(self.hint(2))
        self.assertEqual(len(r.run_once()), 1)
        self.assertEqual(len(calls), 2)

    def test_loop_stops_on_exit(self):
        """The loop terminates when the exit event is set."""
        self.cfg.interval = 0.01
        r = self.rebalancer()
        ee = threading.Event()
        thread = threading.Thread(target=r.loop, args=(ee,))
        thread.start()
        ee.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    @mock.patch.object(rebalance, "link_handler")
    def test_evict_peer_uses_removal_path(self, link_handler_mock):
        """Eviction removes the peer with route and FDB entries via link_handler."""
        rebalance._evict_peer("wg-welt", "KEY=")
        link_handler_mock.assert_called_once_with(
            rebalance.WireGuardClient(public_key="KEY=", domain="welt", remove=True)
        )


if __name__ == "__main__":
    unittest.main()
