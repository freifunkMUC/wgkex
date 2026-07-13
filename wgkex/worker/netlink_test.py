"""Unit tests for netlink.py"""

# pyroute2 decides imports based on platform. WireGuard is specific to Linux only. Mock pyroute2.WireGuard so that
# any testing platform can execute tests.
import importlib
import sys
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

import pyroute2.netlink.exceptions as pyroute2_netlink_exceptions

pyroute2_module_mock = mock.MagicMock()
pyroute2_module_mock.netlink.exceptions = pyroute2_netlink_exceptions
sys.modules["pyroute2"] = pyroute2_module_mock
sys.modules["pyroute2.netlink"] = mock.MagicMock()
from pyroute2 import IPRoute, WireGuard

from wgkex.worker import observability

sys.modules.pop("wgkex.worker.netlink", None)
netlink = importlib.import_module("wgkex.worker.netlink")

_WG_CLIENT_ADD = netlink.WireGuardClient(
    public_key="public_key", domain="add", remove=False
)
_WG_CLIENT_DEL = netlink.WireGuardClient(
    public_key="public_key", domain="del", remove=True
)


def _get_peer_mock(public_key, last_handshake_time, has_allowed_ips=True):
    def peer_get_attr(attr: str):
        if attr == "WGPEER_A_LAST_HANDSHAKE_TIME":
            return {"tv_sec": last_handshake_time}
        if attr == "WGPEER_A_PUBLIC_KEY":
            return public_key.encode()
        if attr == "WGPEER_A_ALLOWEDIPS":
            if not has_allowed_ips:
                return None
            return [
                {
                    "attrs": [
                        ("WGALLOWEDIP_A_CIDR_MASK", 63),
                        ("WGALLOWEDIP_A_FAMILY", 10),
                        (
                            "WGALLOWEDIP_A_IPADDR",
                            "20:01:0d:b8:0e:d0:00:0a:00:00:00:00:00:00:00:00",
                        ),
                    ],
                    "addr": "2001:db8:ed0:a::/63",
                }
            ]

    peer_mock = mock.Mock()
    peer_mock.get_attr.side_effect = peer_get_attr
    return peer_mock


def _get_wg_mock(public_key, last_handshake_time, has_allowed_ips=True):
    peer_mock = _get_peer_mock(public_key, last_handshake_time, has_allowed_ips)

    def msg_get_attr(attr: str):
        if attr == "WGDEVICE_A_PEERS":
            return [peer_mock]

    msg_mock = mock.Mock()
    msg_mock.get_attr.side_effect = msg_get_attr
    wg_instance = WireGuard()
    wg_info_mock = wg_instance
    wg_info_mock.set.return_value = {"WireGuard": "set"}
    wg_info_mock.info.return_value = [msg_mock]
    return wg_info_mock


class NetlinkTest(unittest.TestCase):
    def setUp(self) -> None:
        netlink.reset_handles()
        IPRoute.reset_mock(return_value=True, side_effect=True)
        WireGuard.reset_mock(return_value=True, side_effect=True)
        self.route_info_mock = IPRoute()
        self.addCleanup(netlink.reset_handles)

    def test_find_stale_wireguard_clients_success_with_non_stale_peer(self):
        """Tests find_stale_wireguard_clients no operation on non-stale peers."""
        _wg_info_mock = _get_wg_mock(
            "WGPEER_A_PUBLIC_KEY",
            int((datetime.now(UTC) - timedelta(seconds=3)).timestamp()),
        )
        self.assertListEqual(
            [], netlink.find_stale_wireguard_clients(False, "some_interface")
        )

    def test_find_stale_wireguard_clients_success_stale_peer(self):
        """Tests find_stale_wireguard_clients removal of stale peer"""
        _wg_info_mock = _get_wg_mock(
            "WGPEER_A_PUBLIC_KEY_STALE",
            int((datetime.now(UTC) - timedelta(hours=5)).timestamp()),
        )
        self.assertListEqual(
            [("WGPEER_A_PUBLIC_KEY_STALE", None)],
            netlink.find_stale_wireguard_clients(False, "some_interface"),
        )

    def test_find_stale_wireguard_clients_custom_timeout(self):
        """The stale timeout is configurable."""
        _get_wg_mock(
            "WGPEER_A_PUBLIC_KEY",
            int((datetime.now(UTC) - timedelta(seconds=120)).timestamp()),
        )
        self.assertListEqual(
            [], netlink.find_stale_wireguard_clients(False, "some_interface", 300)
        )
        self.assertListEqual(
            [("WGPEER_A_PUBLIC_KEY", None)],
            netlink.find_stale_wireguard_clients(False, "some_interface", 60),
        )

    def test_wg_flush_stale_peers_keep(self):
        """Peers matched by keep are not removed."""
        _get_wg_mock(
            "WGPEER_A_PUBLIC_KEY_STALE",
            int((datetime.now(UTC) - timedelta(hours=5)).timestamp()),
        )
        self.assertListEqual(
            [],
            netlink.wg_flush_stale_peers(
                False, "domain", keep=lambda k: k == "WGPEER_A_PUBLIC_KEY_STALE"
            ),
        )

    def test_stale_parker_peer_without_allowed_ips_does_not_abort_search(self):
        _get_wg_mock(
            "PARKER_PUBLIC_KEY",
            int((datetime.now() - timedelta(hours=1)).timestamp()),
            has_allowed_ips=False,
        )

        self.assertListEqual(
            [("PARKER_PUBLIC_KEY", None)],
            netlink.find_stale_wireguard_clients(True, "wg-nodes"),
        )

    def test_route_handler_add_success(self):
        """Test route_handler for normal add operation."""
        self.route_info_mock.route.return_value = {"key": "value"}
        self.assertDictEqual({"key": "value"}, netlink.route_handler(_WG_CLIENT_ADD))
        self.route_info_mock.route.assert_called_with(
            "replace", dst="fe80::282:6eff:fe9d:ecd3/128", oif=mock.ANY
        )

    def test_route_handler_remove_success(self):
        """Test route_handler for normal del operation."""
        self.route_info_mock.route.return_value = {"key": "value"}
        self.assertDictEqual({"key": "value"}, netlink.route_handler(_WG_CLIENT_DEL))
        self.route_info_mock.route.assert_called_with(
            "del", dst="fe80::282:6eff:fe9d:ecd3/128", oif=mock.ANY
        )

    def test_update_wireguard_peer_success(self):
        """Test update_wireguard_peer for normal operation."""
        wg_info_mock = _get_wg_mock(
            "WGPEER_A_PUBLIC_KEY",
            int((datetime.now(UTC) - timedelta(seconds=3)).timestamp()),
        )
        self.assertDictEqual(
            {"WireGuard": "set"}, netlink.update_wireguard_peer(_WG_CLIENT_ADD)
        )
        wg_info_mock.set.assert_called_with(
            interface="wg-add",
            peer={
                "public_key": "public_key",
                "allowed_ips": ["fe80::282:6eff:fe9d:ecd3/128"],
                "remove": False,
            },
        )

    def test_bridge_fdb_handler_append_success(self):
        """Test bridge_fdb_handler for normal append operation."""
        self.route_info_mock.fdb.return_value = {"key": "value"}
        self.assertEqual({"key": "value"}, netlink.bridge_fdb_handler(_WG_CLIENT_ADD))
        self.route_info_mock.fdb.assert_called_with(
            "append",
            lladdr="00:00:00:00:00:00",
            dst="fe80::282:6eff:fe9d:ecd3",
            ifindex=mock.ANY,
            NDA_IFINDEX=mock.ANY,
        )

    def test_bridge_fdb_handler_del_success(self):
        """Test bridge_fdb_handler for normal del operation."""
        self.route_info_mock.fdb.return_value = {"key": "value"}
        self.assertEqual({"key": "value"}, netlink.bridge_fdb_handler(_WG_CLIENT_DEL))
        self.route_info_mock.fdb.assert_called_with(
            "del",
            ifindex=mock.ANY,
            NDA_IFINDEX=mock.ANY,
            lladdr="00:00:00:00:00:00",
            dst="fe80::282:6eff:fe9d:ecd3",
        )

    def test_link_handler_addition_success(self):
        """Test link_handler for normal operation."""
        expected = {
            "Wireguard": {"WireGuard": "set"},
            "Route": {"IPRoute": "route"},
            "Bridge FDB": {"IPRoute": "fdb"},
        }
        wg_info_mock = _get_wg_mock(
            "WGPEER_A_PUBLIC_KEY",
            int((datetime.now(UTC) - timedelta(seconds=3)).timestamp()),
        )
        wg_info_mock.set.return_value = {"WireGuard": "set"}
        self.route_info_mock.fdb.return_value = {"IPRoute": "fdb"}
        self.route_info_mock.route.return_value = {"IPRoute": "route"}
        self.assertEqual(expected, netlink.link_handler(_WG_CLIENT_ADD))
        self.route_info_mock.fdb.assert_called_with(
            "append",
            ifindex=mock.ANY,
            NDA_IFINDEX=mock.ANY,
            lladdr="00:00:00:00:00:00",
            dst="fe80::282:6eff:fe9d:ecd3",
        )
        self.route_info_mock.route.assert_called_with(
            "replace", dst="fe80::282:6eff:fe9d:ecd3/128", oif=mock.ANY
        )
        wg_info_mock.set.assert_called_with(
            interface="wg-add",
            peer={
                "public_key": "public_key",
                "allowed_ips": ["fe80::282:6eff:fe9d:ecd3/128"],
                "remove": False,
            },
        )

    def test_wg_flush_stale_peers_not_stale_success(self):
        """Tests processing of non-stale WireGuard Peer."""
        _wg_info_mock = _get_wg_mock(
            "WGPEER_A_PUBLIC_KEY",
            int((datetime.now(UTC) - timedelta(seconds=3)).timestamp()),
        )
        self.route_info_mock.fdb.return_value = {"IPRoute": "fdb"}
        self.route_info_mock.route.return_value = {"IPRoute": "route"}
        self.assertListEqual([], netlink.wg_flush_stale_peers(False, "domain"))
        # TODO(ruairi): Understand why pyroute.WireGuard.set
        # wg_info_mock.set.assert_not_called()

    def test_wg_flush_stale_peers_stale_success(self):
        """Tests processing of stale WireGuard Peer."""
        expected = [
            {
                "Wireguard": {"WireGuard": "set"},
                "Route": {"IPRoute": "route"},
                "Bridge FDB": {"IPRoute": "fdb"},
            }
        ]
        self.route_info_mock.fdb.return_value = {"IPRoute": "fdb"}
        self.route_info_mock.route.return_value = {"IPRoute": "route"}
        wg_info_mock = _get_wg_mock(
            "WGPEER_A_PUBLIC_KEY_STALE",
            int((datetime.now(UTC) - timedelta(hours=5)).timestamp()),
        )
        wg_info_mock.set.return_value = {"WireGuard": "set"}
        self.assertListEqual(expected, netlink.wg_flush_stale_peers(False, "domain"))
        self.route_info_mock.route.assert_called_with(
            "del", dst="fe80::281:16ff:fe49:395e/128", oif=mock.ANY
        )

    def test_get_connected_peers_count_success(self):
        """Tests getting the correct number of connected peers for an interface."""
        peers = []
        for i in range(10):
            peer_mock = _get_peer_mock(
                "TEST_KEY",
                int((datetime.now(UTC) - timedelta(minutes=i, seconds=5)).timestamp()),
            )
            peers.append(peer_mock)

        def msg_get_attr(attr: str):
            if attr == "WGDEVICE_A_PEERS":
                return peers

        msg_mock = mock.Mock()
        msg_mock.get_attr.side_effect = msg_get_attr

        wg_instance = WireGuard()
        wg_info_mock = wg_instance
        wg_info_mock.info.return_value = [msg_mock]

        ret = netlink.get_connected_peers_count("wg-welt")
        self.assertEqual(ret, 3)

    def test_get_connected_peers_success(self):
        """Tests getting the public keys of connected peers only."""
        now = datetime.now(UTC)
        peers = [
            _get_peer_mock("CONNECTED", int((now - timedelta(seconds=5)).timestamp())),
            _get_peer_mock("STALE", int((now - timedelta(minutes=10)).timestamp())),
        ]

        def msg_get_attr(attr: str):
            if attr == "WGDEVICE_A_PEERS":
                return peers

        msg_mock = mock.Mock()
        msg_mock.get_attr.side_effect = msg_get_attr
        wg_info_mock = WireGuard()
        wg_info_mock.info.return_value = [msg_mock]

        self.assertListEqual(["CONNECTED"], netlink.get_connected_peers("wg-welt"))

    @mock.patch("pyroute2.WireGuard")
    def test_get_connected_peers_count_NetlinkDumpInterrupted(self, pyroute2_wg_mock):
        """Tests getting the correct number of connected peers for an interface."""

        nl_wg_mock_ctx = mock.MagicMock()
        wg_info_mock = mock.MagicMock(
            side_effect=(pyroute2_netlink_exceptions.NetlinkDumpInterrupted),
        )
        nl_wg_mock_ctx.info = wg_info_mock

        pyroute2_wg_mock.return_value = nl_wg_mock_ctx

        self.assertRaises(
            pyroute2_netlink_exceptions.NetlinkDumpInterrupted,
            netlink.get_connected_peers_count,
            "wg-welt",
        )
        self.assertTrue(len(wg_info_mock.mock_calls) == 2)

    def test_get_device_data_success(self):
        def msg_get_attr(attr: str):
            if attr == "WGDEVICE_A_LISTEN_PORT":
                return 51820
            if attr == "WGDEVICE_A_PUBLIC_KEY":
                return "TEST_PUBLIC_KEY".encode("ascii")

        msg_mock = mock.Mock()
        msg_mock.get_attr.side_effect = msg_get_attr

        wg_instance = WireGuard()
        wg_info_mock = wg_instance
        wg_info_mock.info.return_value = [msg_mock]

        ret = netlink.get_device_data("wg-welt")
        self.assertTupleEqual(ret, (51820, "TEST_PUBLIC_KEY", mock.ANY))

    def test_handles_are_reused(self):
        """Netlink sockets and interface indexes are kept across operations."""
        IPRoute.reset_mock()
        netlink.route_handler(_WG_CLIENT_ADD)
        netlink.route_handler(_WG_CLIENT_ADD)
        netlink.bridge_fdb_handler(_WG_CLIENT_ADD)
        self.assertEqual(1, IPRoute.call_count)
        # wg-add and vx-add, each looked up once
        self.assertEqual(2, self.route_info_mock.link_lookup.call_count)

    def test_reconnect_on_socket_error(self):
        """A broken socket is replaced and the operation retried once."""
        self.route_info_mock.route.side_effect = [OSError("broken"), {"ok": 1}]
        reconnects = observability.REGISTRY.get_sample_value(
            "wgkex_worker_netlink_reconnects_total"
        )
        route_ops = observability.REGISTRY.get_sample_value(
            "wgkex_worker_netlink_operation_seconds_count", {"op": "route"}
        )
        self.assertEqual({"ok": 1}, netlink.route_handler(_WG_CLIENT_ADD))
        self.route_info_mock.close.assert_called_once()
        self.assertEqual(
            reconnects + 1,
            observability.REGISTRY.get_sample_value(
                "wgkex_worker_netlink_reconnects_total"
            ),
        )
        # One observation per call, including the retry
        self.assertEqual(
            (route_ops or 0) + 1,
            observability.REGISTRY.get_sample_value(
                "wgkex_worker_netlink_operation_seconds_count", {"op": "route"}
            ),
        )
        self.assertEqual(2, self.route_info_mock.route.call_count)

    def test_refresh_ifindex_on_enodev(self):
        """Recreated interfaces get their index looked up again."""
        import errno

        self.route_info_mock.route.side_effect = [
            pyroute2_netlink_exceptions.NetlinkError(errno.ENODEV),
            {"ok": 1},
        ]
        self.assertEqual({"ok": 1}, netlink.route_handler(_WG_CLIENT_ADD))
        self.assertEqual(2, self.route_info_mock.link_lookup.call_count)
        self.route_info_mock.close.assert_not_called()

    def test_other_netlink_errors_are_raised(self):
        """Request errors are not retried."""
        self.route_info_mock.route.side_effect = (
            pyroute2_netlink_exceptions.NetlinkError(17)
        )
        self.assertRaises(
            pyroute2_netlink_exceptions.NetlinkError,
            netlink.route_handler,
            _WG_CLIENT_ADD,
        )
        self.assertEqual(1, self.route_info_mock.route.call_count)
    def test_find_stale_wireguard_clients_parker_stale_peer(self):
        """Tests find_stale_wireguard_clients in parker mode returning range6."""

        _ = _get_wg_mock(
            "PARKER_PUBLIC_KEY", int((datetime.now() - timedelta(hours=1)).timestamp())
        )

        self.assertListEqual(
            [("PARKER_PUBLIC_KEY", "2001:db8:ed0:a::/63")],
            netlink.find_stale_wireguard_clients(True, "wg-nodes"),
        )

    def test_wg_flush_stale_peers_parker_stale_success(self):
        """Tests processing of stale WireGuard Peer in parker mode."""

        _ = _get_wg_mock(
            "PARKER_PUBLIC_KEY", int((datetime.now() - timedelta(hours=1)).timestamp())
        )

        expected = [
            {
                "Wireguard": {"WireGuard": "set"},
                "Route": {"IPRoute": "route"},
            }
        ]

        ret = netlink.wg_flush_stale_peers(True, "parker")
        self.assertListEqual(expected, ret)

        # Verify the stale peer resulted in a route deletion for the parker range
        self.route_info_mock.route.assert_called_with(
            "del", dst="2001:db8:ed0:a::/63", oif=mock.ANY
        )


if __name__ == "__main__":
    unittest.main()
