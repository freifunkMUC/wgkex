"""Unit tests for netlink.py"""

# pyroute2 decides imports based on platform. WireGuard is specific to Linux only. Mock pyroute2.WireGuard so that
# any testing platform can execute tests.
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

from wgkex.worker import netlink  # noqa: E402

_WG_CLIENT_ADD = netlink.WireGuardClient(
    public_key="public_key", domain="add", remove=False
)
_WG_CLIENT_DEL = netlink.WireGuardClient(
    public_key="public_key", domain="del", remove=True
)


def _get_peer_mock(public_key, last_handshake_time):
    def peer_get_attr(attr: str):
        if attr == "WGPEER_A_LAST_HANDSHAKE_TIME":
            return {"tv_sec": last_handshake_time}
        if attr == "WGPEER_A_PUBLIC_KEY":
            return public_key.encode()

    peer_mock = mock.Mock()
    peer_mock.get_attr.side_effect = peer_get_attr
    return peer_mock


def _get_wg_mock(public_key, last_handshake_time):
    peer_mock = _get_peer_mock(public_key, last_handshake_time)

    def msg_get_attr(attr: str):
        if attr == "WGDEVICE_A_PEERS":
            return [peer_mock]

    msg_mock = mock.Mock()
    msg_mock.get_attr.side_effect = msg_get_attr
    wg_instance = WireGuard()
    wg_info_mock = wg_instance.__enter__.return_value
    wg_info_mock.set.return_value = {"WireGuard": "set"}
    wg_info_mock.info.return_value = [msg_mock]
    return wg_info_mock


class NetlinkTest(unittest.TestCase):
    def setUp(self) -> None:
        iproute_instance = IPRoute()
        self.route_info_mock = iproute_instance.__enter__.return_value
        # self.addCleanup(mock.patch.stopall)

    def test_find_stale_wireguard_clients_success_with_non_stale_peer(self):
        """Tests find_stale_wireguard_clients no operation on non-stale peers."""
        _wg_info_mock = _get_wg_mock(
            "WGPEER_A_PUBLIC_KEY",
            int((datetime.now() - timedelta(seconds=3)).timestamp()),
        )
        self.assertListEqual([], netlink.find_stale_wireguard_clients("some_interface"))

    def test_find_stale_wireguard_clients_success_stale_peer(self):
        """Tests find_stale_wireguard_clients removal of stale peer"""
        _wg_info_mock = _get_wg_mock(
            "WGPEER_A_PUBLIC_KEY_STALE",
            int((datetime.now() - timedelta(hours=5)).timestamp()),
        )
        self.assertListEqual(
            ["WGPEER_A_PUBLIC_KEY_STALE"],
            netlink.find_stale_wireguard_clients("some_interface"),
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
            int((datetime.now() - timedelta(seconds=3)).timestamp()),
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
            int((datetime.now() - timedelta(seconds=3)).timestamp()),
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
            int((datetime.now() - timedelta(seconds=3)).timestamp()),
        )
        self.route_info_mock.fdb.return_value = {"IPRoute": "fdb"}
        self.route_info_mock.route.return_value = {"IPRoute": "route"}
        self.assertListEqual([], netlink.wg_flush_stale_peers("domain"))
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
            int((datetime.now() - timedelta(hours=5)).timestamp()),
        )
        wg_info_mock.set.return_value = {"WireGuard": "set"}
        self.assertListEqual(expected, netlink.wg_flush_stale_peers("domain"))
        self.route_info_mock.route.assert_called_with(
            "del", dst="fe80::281:16ff:fe49:395e/128", oif=mock.ANY
        )

    def test_get_connected_peers_count_success(self):
        """Tests getting the correct number of connected peers for an interface."""
        peers = []
        for i in range(10):
            peer_mock = _get_peer_mock(
                "TEST_KEY",
                int((datetime.now() - timedelta(minutes=i, seconds=5)).timestamp()),
            )
            peers.append(peer_mock)

        def msg_get_attr(attr: str):
            if attr == "WGDEVICE_A_PEERS":
                return peers

        msg_mock = mock.Mock()
        msg_mock.get_attr.side_effect = msg_get_attr

        wg_instance = WireGuard()
        wg_info_mock = wg_instance.__enter__.return_value
        wg_info_mock.info.return_value = [msg_mock]

        ret = netlink.get_connected_peers_count("wg-welt")
        self.assertEqual(ret, 3)

    @mock.patch("pyroute2.WireGuard")
    def test_get_connected_peers_count_NetlinkDumpInterrupted(self, pyroute2_wg_mock):
        """Tests getting the correct number of connected peers for an interface."""

        nl_wg_mock_ctx = mock.MagicMock()
        wg_info_mock = mock.MagicMock(
            side_effect=(pyroute2_netlink_exceptions.NetlinkDumpInterrupted),
        )
        nl_wg_mock_ctx.info = wg_info_mock

        nl_wg_mock_inst = pyroute2_wg_mock.return_value
        nl_wg_mock_inst.__enter__ = mock.MagicMock(return_value=nl_wg_mock_ctx)

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
        wg_info_mock = wg_instance.__enter__.return_value
        wg_info_mock.info.return_value = [msg_mock]

        ret = netlink.get_device_data("wg-welt")
        self.assertTupleEqual(ret, (51820, "TEST_PUBLIC_KEY", mock.ANY))


if __name__ == "__main__":
    unittest.main()
