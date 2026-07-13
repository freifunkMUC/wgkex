"""Behavioral tests for worker netlink cleanup."""

import errno
import importlib
import sys
import unittest
from datetime import datetime, timedelta

import mock
import pyroute2.netlink.exceptions as pyroute2_netlink_exceptions

pyroute2_module_mock = mock.MagicMock()
pyroute2_module_mock.netlink.exceptions = pyroute2_netlink_exceptions
sys.modules["pyroute2"] = pyroute2_module_mock
sys.modules["pyroute2.netlink"] = mock.MagicMock()
from pyroute2 import IPRoute, WireGuard  # noqa: E402

sys.modules.pop("wgkex.worker.netlink", None)
netlink = importlib.import_module("wgkex.worker.netlink")
from wgkex.worker.peer_state import PeerMutationCoordinator  # noqa: E402


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


class NetlinkTest(unittest.TestCase):
    def setUp(self):
        self.wg = WireGuard().__enter__.return_value
        self.wg.reset_mock()
        self.wg.info.side_effect = None
        self.ip = IPRoute().__enter__.return_value
        self.ip.reset_mock()
        self.ip.link_lookup.return_value = [7]

    def _find(self, peers, parker=False, clock=None, coordinator=None, **kwargs):
        clock = clock or MutableClock()
        coordinator = coordinator or PeerMutationCoordinator(clock=clock)
        self.wg.info.return_value = [_message(peers)]
        return netlink.find_stale_wireguard_clients(
            parker,
            "wg-nodes" if parker else "wg-domain",
            coordinator=coordinator,
            clock=clock,
            **kwargs,
        )

    def test_handshake_cutoff_is_inclusive_and_mode_specific(self):
        clock = MutableClock()
        parker = self._find(
            [
                _peer("recent", clock.now - 299),
                _peer("boundary", clock.now - 300),
            ],
            parker=True,
            clock=clock,
            stale_timeout=300,
        )
        self.assertEqual([peer.public_key for peer in parker], ["boundary"])

        legacy = self._find(
            [
                _peer("recent", clock.now - 10799),
                _peer("boundary", clock.now - 10800),
            ],
            clock=clock,
            stale_timeout=10800,
        )
        self.assertEqual([peer.public_key for peer in legacy], ["boundary"])

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
                parker=True,
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
                parker=True,
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
            parker=True,
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
                _peer("valid", 1),
            ],
            parker=True,
            stale_timeout=300,
        )
        self.assertEqual([peer.public_key for peer in result], ["valid"])

    def test_stale_parker_peer_without_allowed_ips_is_skipped(self):
        result = self._find(
            [
                _peer("without-allowed-ips", 1, None),
                _peer("valid", 1),
            ],
            parker=True,
            stale_timeout=300,
        )
        self.assertEqual([peer.public_key for peer in result], ["valid"])

    def test_netlink_dump_interruption_retries_once(self):
        clock = MutableClock()
        coordinator = PeerMutationCoordinator(clock=clock)
        self.wg.info.side_effect = [
            pyroute2_netlink_exceptions.NetlinkDumpInterrupted(),
            [_message([_peer("key", 1)])],
        ]
        result = netlink.find_stale_wireguard_clients(
            False,
            "wg-domain",
            stale_timeout=300,
            coordinator=coordinator,
            clock=clock,
        )
        self.assertEqual(result[0].public_key, "key")
        self.assertEqual(self.wg.info.call_count, 2)

        self.wg.info.reset_mock()
        self.wg.info.side_effect = pyroute2_netlink_exceptions.NetlinkDumpInterrupted()
        with self.assertRaises(pyroute2_netlink_exceptions.NetlinkDumpInterrupted):
            netlink.find_stale_wireguard_clients(
                False,
                "wg-domain",
                coordinator=coordinator,
                clock=clock,
            )
        self.assertEqual(self.wg.info.call_count, 2)

    def test_parker_and_legacy_deletion_order(self):
        for client, expected in (
            (
                netlink.ParkerWireGuardClient("key", "2001:db8::/63", True),
                ["route", "wireguard"],
            ),
            (
                netlink.WireGuardClient("key", "domain", True),
                ["bridge_fdb", "route", "wireguard"],
            ),
        ):
            calls = []
            with (
                mock.patch.object(
                    netlink,
                    "bridge_fdb_handler",
                    side_effect=lambda _: calls.append("bridge_fdb") or {},
                ),
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
                outcomes = netlink.link_handler(client)
            self.assertEqual(calls, expected)
            self.assertEqual(list(outcomes), expected)
            self.assertTrue(
                all(outcome.status == "deleted" for outcome in outcomes.values())
            )

    def test_parker_shared_prefix_preserves_route(self):
        client = netlink.ParkerWireGuardClient("stale", "2001:db8::/63", True)
        with (
            mock.patch.object(netlink, "route_handler") as route,
            mock.patch.object(netlink, "update_wireguard_peer", return_value={}),
        ):
            outcomes = netlink.link_handler(client, preserve_parker_route=True)
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
                    _peer(
                        "stale",
                        1,
                        [{"addr": "2001:db8:20::/63"}],
                    ),
                    _peer(
                        "active",
                        100_000,
                        [{"addr": "2001:db8:20::/63"}],
                    ),
                ]
            )
        ]
        self.assertTrue(
            netlink.parker_prefix_owned_by_other_peer(
                "wg-nodes", "2001:db8:20::/63", "stale", 63
            )
        )

    def test_current_parker_prefixes_are_discovered_for_update(self):
        self.wg.info.return_value = [
            _message(
                [
                    _peer(
                        "other",
                        1,
                        [{"addr": "2001:db8:10::/63"}],
                    ),
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
        client = netlink.WireGuardClient("key", "domain", True)
        with (
            mock.patch.object(netlink, "bridge_fdb_handler", return_value={}),
            mock.patch.object(
                netlink, "route_handler", side_effect=RuntimeError("route failed")
            ),
            mock.patch.object(netlink, "update_wireguard_peer") as remove_peer,
            self.assertRaises(netlink.PeerMutationError) as raised,
        ):
            netlink.link_handler(client)

        self.assertEqual(raised.exception.operation, "route")
        self.assertEqual(list(raised.exception.operations), ["bridge_fdb"])
        remove_peer.assert_not_called()

    def test_already_absent_dependency_is_idempotent(self):
        client = netlink.ParkerWireGuardClient("key", "2001:db8::/63", True)
        absent = pyroute2_netlink_exceptions.NetlinkError(errno.ENOENT)
        with (
            mock.patch.object(netlink, "route_handler", side_effect=absent),
            mock.patch.object(netlink, "update_wireguard_peer", return_value={}),
        ):
            outcomes = netlink.link_handler(client)
        self.assertEqual(outcomes["route"].status, "already_absent")
        self.assertEqual(outcomes["wireguard"].status, "deleted")

    def test_failed_cleanup_is_selected_again_on_next_sweep(self):
        stale = netlink.StaleWireGuardPeer("key", "2001:db8::/63", "stale")
        client = netlink.ParkerWireGuardClient("key", "2001:db8::/63", True)
        failure = netlink.PeerMutationError("route", client, {}, RuntimeError("failed"))
        with (
            mock.patch.object(
                netlink, "find_stale_wireguard_clients", return_value=[stale]
            ) as find,
            mock.patch.object(
                netlink,
                "link_handler",
                side_effect=[
                    failure,
                    {
                        "route": netlink.OperationOutcome("deleted"),
                        "wireguard": netlink.OperationOutcome("deleted"),
                    },
                ],
            ),
        ):
            first = netlink.wg_flush_stale_peers(True)
            second = netlink.wg_flush_stale_peers(True)

        self.assertEqual(first[0].status, "failed")
        self.assertEqual(first[0].failed_operation, "route")
        self.assertEqual(second[0].status, "deleted")
        self.assertEqual(find.call_count, 4)

    def test_peer_that_becomes_active_after_scan_is_deferred(self):
        stale = netlink.StaleWireGuardPeer("key", None, "stale")
        with (
            mock.patch.object(
                netlink,
                "find_stale_wireguard_clients",
                side_effect=[[stale], []],
            ),
            mock.patch.object(netlink, "link_handler") as delete_peer,
        ):
            result = netlink.wg_flush_stale_peers(False, "domain")
        self.assertEqual(result[0].status, "deferred")
        delete_peer.assert_not_called()

    def test_pending_old_parker_route_is_retried(self):
        coordinator = PeerMutationCoordinator()
        coordinator.record_pending_parker_route("2001:db8:1::/63")
        with (
            mock.patch.object(
                netlink,
                "parker_prefix_owned_by_other_peer",
                return_value=False,
            ),
            mock.patch.object(netlink, "route_handler", return_value={}) as route,
        ):
            netlink.retry_pending_parker_routes(
                coordinator=coordinator, prefix_length=63
            )
        route.assert_called_once()
        self.assertEqual(coordinator.pending_parker_routes(), ())

    def test_route_fdb_and_wireguard_handlers(self):
        legacy = netlink.WireGuardClient("public_key", "domain", True)
        self.ip.route.return_value = {"route": "deleted"}
        self.ip.fdb.return_value = {"fdb": "deleted"}
        self.wg.set.return_value = {"peer": "deleted"}

        self.assertEqual(netlink.route_handler(legacy), {"route": "deleted"})
        self.assertEqual(netlink.bridge_fdb_handler(legacy), {"fdb": "deleted"})
        self.assertEqual(netlink.update_wireguard_peer(legacy), {"peer": "deleted"})
        self.ip.route.assert_called_with("del", dst=legacy.lladdr, oif=mock.ANY)
        self.ip.fdb.assert_called_with(
            "del",
            ifindex=mock.ANY,
            lladdr="00:00:00:00:00:00",
            dst=legacy.lladdr.removesuffix("/128"),
            NDA_IFINDEX=mock.ANY,
        )

    def test_connected_count_and_device_data(self):
        now = datetime.now()
        peers = [
            _peer(
                f"key-{minutes}",
                int((now - timedelta(minutes=minutes)).timestamp()),
            )
            for minutes in range(5)
        ]
        self.wg.info.return_value = [_message(peers)]
        self.assertEqual(netlink.get_connected_peers_count("wg-domain"), 3)

        self.ip.get_addr.return_value = [
            mock.Mock(get_attr=mock.Mock(return_value="fe80::1"))
        ]
        self.wg.info.return_value = [_message([])]
        self.assertEqual(
            netlink.get_device_data("wg-domain"),
            (51820, "device-key", "fe80::1"),
        )


if __name__ == "__main__":
    unittest.main()
