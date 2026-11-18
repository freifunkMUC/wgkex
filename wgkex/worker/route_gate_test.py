"""Tests for the handshake-gated Parker routes."""

import contextlib
import threading
import unittest
from unittest import mock

from wgkex.worker import route_gate


class _Nla(dict):
    def get_attr(self, name):
        return self.get(name)


def _peer(key, prefix, handshake):
    return _Nla(
        WGPEER_A_PUBLIC_KEY=key.encode(),
        WGPEER_A_LAST_HANDSHAKE_TIME={"tv_sec": handshake},
        WGPEER_A_ALLOWEDIPS=[{"addr": prefix}],
    )


class _Coordinator:
    @contextlib.contextmanager
    def peer_lock(self, *_):
        yield


class RouteGateTest(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.peers = []
        self.routes = {"2001:db8:9::/63"}
        self.routed = []
        ip = mock.MagicMock()
        ip.get_routes.side_effect = lambda **_: [
            _Nla({"dst_len": 63}, RTA_DST=route.split("/")[0]) for route in self.routes
        ]

        def route(action, dst, oif):
            self.assertEqual(oif, 7)
            if action == "del":
                self.routes.discard(dst)
            else:
                self.routes.add(dst)

        ip.route.side_effect = route
        handles = mock.MagicMock()
        handles.iproute.return_value = ip
        handles.link_index.return_value = 7
        for name, value in (
            ("_wireguard_info", lambda _: [_Nla(WGDEVICE_A_PEERS=self.peers)]),
            ("_netlink_call", lambda op, _name="other": op(handles)),
        ):
            patcher = mock.patch.object(route_gate, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.gate = route_gate.RouteGate(
            180,
            63,
            lambda key, handshake: self.routed.append((key, handshake)),
            coordinator=_Coordinator(),
            clock=lambda: self.now,
        )

    def test_routes_follow_fresh_handshakes(self):
        self.peers = [
            _peer("fresh", "2001:db8:1::/63", 990),
            _peer("stale", "2001:db8:2::/63", 500),
        ]

        self.assertEqual(self.gate.sync(), ["fresh"])
        self.assertEqual(self.routes, {"2001:db8:1::/63"})
        self.assertEqual(self.routed, [("fresh", 990)])
        # A second run does not add the route again.
        self.assertEqual(self.gate.sync(), [])

    def test_unroute_holds_route_until_newer_handshake(self):
        self.peers = [_peer("node", "2001:db8:1::/63", 990)]
        self.gate.sync()

        self.gate.unroute("node")
        self.assertEqual(self.routes, set())
        self.gate.sync()
        self.assertEqual(self.routes, set())

        self.now = 1030
        self.peers = [_peer("node", "2001:db8:1::/63", 1020)]
        self.assertEqual(self.gate.sync(), ["node"])
        self.assertEqual(self.routes, {"2001:db8:1::/63"})

    def test_unroute_with_handshake_of_new_pgw(self):
        # The node moved to another pgw at 995: remove the old route at once.
        self.peers = [_peer("node", "2001:db8:1::/63", 990)]
        self.gate.sync()
        self.gate.unroute("node", 995)
        self.assertEqual(self.routes, set())

        # The node moved back before the late message arrived: keep the route.
        self.peers = [_peer("node", "2001:db8:1::/63", 998)]
        self.gate.sync()
        self.gate.unroute("node", 995)
        self.assertEqual(self.routes, {"2001:db8:1::/63"})

        # A handshake time in the future (clock skew) holds until now only.
        self.gate.unroute("node", 5000)
        self.assertEqual(self.routes, set())
        self.now = 1010
        self.peers = [_peer("node", "2001:db8:1::/63", 1005)]
        self.assertEqual(self.gate.sync(), ["node"])

    def test_run_continues_after_errors(self):
        exit_event = threading.Event()
        calls = []

        def sync():
            calls.append(1)
            if len(calls) == 2:
                exit_event.set()
            raise RuntimeError("netlink")

        self.gate.sync = sync
        self.gate.run(exit_event, 0)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
