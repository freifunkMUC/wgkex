"""Behavioral tests for the Parker stale peer cleanup in netlink.py."""

import errno
import importlib
import sys
import unittest
from unittest import mock

import pyroute2.netlink.exceptions as pyroute2_netlink_exceptions

pyroute2_module_mock = mock.MagicMock()
pyroute2_module_mock.netlink.exceptions = pyroute2_netlink_exceptions
sys.modules["pyroute2"] = pyroute2_module_mock
sys.modules["pyroute2.netlink"] = mock.MagicMock()
from pyroute2 import IPRoute, WireGuard

sys.modules.pop("wgkex.worker.netlink", None)
netlink = importlib.import_module("wgkex.worker.netlink")
from wgkex.worker.peer_state import PeerMutationCoordinator


class MutableClock:
    def __init__(self, now=100_000):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


_DEFAULT_ALLOWED_IPS = object()


def _peer(
    public_key="key",
    handshake=100_000,
    allowed_ips=_DEFAULT_ALLOWED_IPS,
):
    attrs = {
        "WGPEER_A_PUBLIC_KEY": (
            public_key.encode("ascii") if isinstance(public_key, str) else public_key
        ),
        "WGPEER_A_LAST_HANDSHAKE_TIME": (
            {"tv_sec": handshake} if handshake is not None else None
        ),
        "WGPEER_A_ALLOWEDIPS": (
            [{"addr": "2001:db8:1::/63"}]
            if allowed_ips is _DEFAULT_ALLOWED_IPS
            else allowed_ips
        ),
    }
    peer = mock.Mock()
    peer.get_attr.side_effect = attrs.get
    return peer


def _message(peers):
    message = mock.Mock()
    message.get_attr.side_effect = {
        "WGDEVICE_A_PEERS": peers,
        "WGDEVICE_A_LISTEN_PORT": 51820,
        "WGDEVICE_A_PUBLIC_KEY": b"device-key",
    }.get
    return message


class ParkerNetlinkTest(unittest.TestCase):
    def setUp(self):
        netlink.reset_handles()
        IPRoute.reset_mock(return_value=True, side_effect=True)
        WireGuard.reset_mock(return_value=True, side_effect=True)
        self.wg = WireGuard()
        self.ip = IPRoute()
        self.ip.link_lookup.return_value = [7]

    def _find(self, peers, clock=None, coordinator=None, **kwargs):
        clock = clock or MutableClock()
        coordinator = coordinator or PeerMutationCoordinator(clock=clock)
        self.wg.info.return_value = [_message(peers)]
        return netlink.find_stale_parker_peers(
            "wg-nodes", coordinator=coordinator, clock=clock, **kwargs
        )

    def test_handshake_cutoff_is_inclusive(self):
        clock = MutableClock()
        result = self._find(
            [
                _peer("recent", clock.now - 299),
                _peer("boundary", clock.now - 300),
            ],
            clock=clock,
            stale_timeout=300,
        )
        self.assertEqual([peer.public_key for peer in result], ["boundary"])

    def test_never_handshaked_peers_get_grace_then_become_stale(self):
        for handshake in (0, None):
            with self.subTest(handshake=handshake):
                clock = MutableClock()
                coordinator = PeerMutationCoordinator(clock=clock)
                peers = [_peer("never", handshake)]
                self.assertEqual(
                    self._find(
                        peers,
                        clock=clock,
                        coordinator=coordinator,
                        initial_handshake_grace=600,
                    ),
                    [],
                )
                clock.advance(600)
                result = self._find(
                    peers,
                    clock=clock,
                    coordinator=coordinator,
                    initial_handshake_grace=600,
                )
                self.assertEqual(result[0].reason, "never_handshaked")

    def test_recent_queue_update_refreshes_grace_for_stale_peer(self):
        clock = MutableClock()
        coordinator = PeerMutationCoordinator(clock=clock)
        coordinator.record_provisioned("key")
        peers = [_peer("key", clock.now - 1000)]

        clock.advance(599)
        self.assertEqual(
            self._find(
                peers,
                clock=clock,
                coordinator=coordinator,
                stale_timeout=300,
                initial_handshake_grace=600,
            ),
            [],
        )
        clock.advance(1)
        self.assertEqual(
            self._find(
                peers,
                clock=clock,
                coordinator=coordinator,
                stale_timeout=300,
                initial_handshake_grace=600,
            )[0].public_key,
            "key",
        )

    def test_parker_prefix_is_selected_from_all_allowed_ips(self):
        result = self._find(
            [
                _peer(
                    "key",
                    1,
                    [
                        {"addr": "fe80::1/128"},
                        {"addr": "192.0.2.1/32"},
                        {"addr": "2001:db8:20::1/63"},
                    ],
                )
            ],
            stale_timeout=300,
        )
        self.assertEqual(result[0].parker_prefix, "2001:db8:20::/63")

    def test_malformed_peers_are_skipped_without_aborting_sweep(self):
        malformed_handshake = _peer("bad-handshake", 1)
        malformed_handshake.get_attr.side_effect = {
            "WGPEER_A_PUBLIC_KEY": b"bad-handshake",
            "WGPEER_A_LAST_HANDSHAKE_TIME": {"tv_sec": "old"},
        }.get
        result = self._find(
            [
                _peer(None, 1),
                malformed_handshake,
                _peer(
                    "ambiguous",
                    1,
                    [
                        {"addr": "2001:db8:1::/63"},
                        {"addr": "2001:db8:2::/63"},
                    ],
                ),
                _peer("without-allowed-ips", 1, None),
                _peer("valid", 1),
            ],
            stale_timeout=300,
        )
        self.assertEqual([peer.public_key for peer in result], ["valid"])

    def test_netlink_dump_interruption_retries_once(self):
        self.wg.info.side_effect = [
            pyroute2_netlink_exceptions.NetlinkDumpInterrupted(),
            [_message([_peer("key", 1)])],
        ]
        result = netlink.find_stale_parker_peers(
            stale_timeout=300,
            coordinator=PeerMutationCoordinator(clock=MutableClock()),
            clock=MutableClock(),
        )
        self.assertEqual(result[0].public_key, "key")
        self.assertEqual(self.wg.info.call_count, 2)

        self.wg.info.reset_mock()
        self.wg.info.side_effect = pyroute2_netlink_exceptions.NetlinkDumpInterrupted()
        with self.assertRaises(pyroute2_netlink_exceptions.NetlinkDumpInterrupted):
            netlink.find_stale_parker_peers(coordinator=PeerMutationCoordinator())
        self.assertEqual(self.wg.info.call_count, 2)

    def test_parker_deletion_removes_route_before_peer(self):
        calls = []
        with (
            mock.patch.object(
                netlink,
                "route_handler",
                side_effect=lambda _: calls.append("route") or {},
            ),
            mock.patch.object(
                netlink,
                "update_wireguard_peer",
                side_effect=lambda _: calls.append("wireguard") or {},
            ),
        ):
            outcomes = netlink.link_handler(
                netlink.ParkerWireGuardClient("key", "2001:db8::/63", True)
            )
        self.assertEqual(calls, ["route", "wireguard"])
        self.assertEqual(list(outcomes), ["route", "wireguard"])
        self.assertTrue(
            all(outcome.status == "deleted" for outcome in outcomes.values())
        )

    def test_parker_shared_prefix_preserves_route(self):
        client = netlink.ParkerWireGuardClient("stale", "2001:db8::/63", True)
        with (
            mock.patch.object(netlink, "route_handler") as route,
            mock.patch.object(netlink, "update_wireguard_peer", return_value={}),
        ):
            outcomes = netlink.parker_link_handler(client, preserve_parker_route=True)
        route.assert_not_called()
        self.assertEqual(outcomes["route"].status, "preserved_shared")
        self.assertEqual(outcomes["wireguard"].status, "deleted")

    def test_parker_reassignment_removes_old_route_after_new_route(self):
        client = netlink.ParkerWireGuardClient(
            "key",
            "2001:db8:2::/63",
            False,
            previous_ranges6=("2001:db8:1::/63",),
        )
        calls = []

        def route_handler(route_client):
            calls.append(
                (
                    "delete" if route_client.remove else "replace",
                    route_client.range6,
                )
            )
            return {}

        with (
            mock.patch.object(
                netlink,
                "update_wireguard_peer",
                side_effect=lambda _: calls.append(("wireguard", None)) or {},
            ),
            mock.patch.object(netlink, "route_handler", side_effect=route_handler),
        ):
            outcomes = netlink.link_handler(client)

        self.assertEqual(
            calls,
            [
                ("wireguard", None),
                ("replace", "2001:db8:2::/63"),
                ("delete", "2001:db8:1::/63"),
            ],
        )
        self.assertEqual(outcomes["old_route:2001:db8:1::/63"].status, "deleted")

    def test_parker_prefix_ownership_checks_all_current_peers(self):
        self.wg.info.return_value = [
            _message(
                [
                    _peer("stale", 1, [{"addr": "2001:db8:20::/63"}]),
                    _peer("active", 100_000, [{"addr": "2001:db8:20::/63"}]),
                ]
            )
        ]
        self.assertTrue(
            netlink.parker_prefix_owned_by_other_peer(
                "wg-nodes", "2001:db8:20::/63", "stale", 63
            )
        )
        self.assertFalse(
            netlink.parker_prefix_owned_by_other_peer(
                "wg-nodes", "2001:db8:30::/63", "stale", 63
            )
        )

    def test_current_parker_prefixes_are_discovered_for_update(self):
        self.wg.info.return_value = [
            _message(
                [
                    _peer("other", 1, [{"addr": "2001:db8:10::/63"}]),
                    _peer(
                        "key",
                        1,
                        [
                            {"addr": "fe80::1/128"},
                            {"addr": "2001:db8:20::1/63"},
                        ],
                    ),
                ]
            )
        ]
        self.assertEqual(
            netlink.get_parker_prefixes_for_peer("wg-nodes", "key", 63),
            ["2001:db8:20::/63"],
        )

    def test_partial_failure_stops_before_peer_and_is_explicit(self):
        client = netlink.ParkerWireGuardClient("key", "2001:db8::/63", True)
        with (
            mock.patch.object(
                netlink, "route_handler", side_effect=RuntimeError("route failed")
            ),
            mock.patch.object(netlink, "update_wireguard_peer") as remove_peer,
            self.assertRaises(netlink.PeerMutationError) as raised,
        ):
            netlink.parker_link_handler(client)

        self.assertEqual(raised.exception.operation, "route")
        self.assertEqual(list(raised.exception.operations), [])
        remove_peer.assert_not_called()

    def test_already_absent_dependency_is_idempotent(self):
        client = netlink.ParkerWireGuardClient("key", "2001:db8::/63", True)
        absent = pyroute2_netlink_exceptions.NetlinkError(errno.ENOENT)
        with (
            mock.patch.object(netlink, "route_handler", side_effect=absent),
            mock.patch.object(netlink, "update_wireguard_peer", return_value={}),
        ):
            outcomes = netlink.parker_link_handler(client)
        self.assertEqual(outcomes["route"].status, "already_absent")
        self.assertEqual(outcomes["wireguard"].status, "deleted")

    def test_failed_cleanup_is_selected_again_on_next_sweep(self):
        stale = netlink.StaleWireGuardPeer("key", "2001:db8::/63", "stale")
        client = netlink.ParkerWireGuardClient("key", "2001:db8::/63", True)
        failure = netlink.PeerMutationError("route", client, {}, RuntimeError("failed"))
        with (
            mock.patch.object(
                netlink, "find_stale_parker_peers", return_value=[stale]
            ) as find,
            mock.patch.object(
                netlink, "parker_prefix_owned_by_other_peer", return_value=False
            ),
            mock.patch.object(
                netlink,
                "parker_link_handler",
                side_effect=[
                    failure,
                    {
                        "route": netlink.OperationOutcome("deleted"),
                        "wireguard": netlink.OperationOutcome("deleted"),
                    },
                ],
            ),
        ):
            coordinator = PeerMutationCoordinator()
            first = netlink.wg_flush_stale_peers(True, coordinator=coordinator)
            second = netlink.wg_flush_stale_peers(True, coordinator=coordinator)

        self.assertEqual(first[0].status, "failed")
        self.assertEqual(first[0].failed_operation, "route")
        self.assertEqual(second[0].status, "deleted")
        self.assertEqual(find.call_count, 4)

    def test_peer_that_becomes_active_after_scan_is_deferred(self):
        stale = netlink.StaleWireGuardPeer("key", "2001:db8::/63", "stale")
        with (
            mock.patch.object(
                netlink, "find_stale_parker_peers", side_effect=[[stale], []]
            ),
            mock.patch.object(netlink, "parker_link_handler") as delete_peer,
        ):
            result = netlink.wg_flush_stale_peers(
                True, coordinator=PeerMutationCoordinator()
            )
        self.assertEqual(result[0].status, "deferred")
        delete_peer.assert_not_called()

    def test_keep_predicate_protects_parker_peers(self):
        stale = netlink.StaleWireGuardPeer("key", "2001:db8::/63", "stale")
        with (
            mock.patch.object(netlink, "find_stale_parker_peers", return_value=[stale]),
            mock.patch.object(netlink, "parker_link_handler") as delete_peer,
        ):
            result = netlink.wg_flush_stale_peers(
                True,
                keep=lambda key: key == "key",
                coordinator=PeerMutationCoordinator(),
            )
        self.assertEqual(result, [])
        delete_peer.assert_not_called()

    def test_pending_old_parker_route_is_retried(self):
        coordinator = PeerMutationCoordinator()
        coordinator.record_pending_parker_route("2001:db8:1::/63")
        with (
            mock.patch.object(
                netlink, "parker_prefix_owned_by_other_peer", return_value=False
            ),
            mock.patch.object(netlink, "route_handler", return_value={}) as route,
        ):
            netlink.retry_pending_parker_routes(
                coordinator=coordinator, prefix_length=63
            )
        route.assert_called_once()
        self.assertEqual(coordinator.pending_parker_routes(), ())


if __name__ == "__main__":
    unittest.main()
