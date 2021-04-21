"""Unit tests for netlink.py"""
import unittest
import mock
from datetime import timedelta
from datetime import datetime

# pyroute2 decides imports based on platform. WireGuard is specific to Linux only. Mock pyroute2.WireGuard so that
# any testing platform can execute tests.
import sys

sys.modules["pyroute2"] = mock.MagicMock()
sys.modules["pyroute2.WireGuard"] = mock.MagicMock()
sys.modules["pyroute2.IPRoute"] = mock.MagicMock()
from pyroute2 import WireGuard
from pyroute2 import IPRoute
import netlink

_WG_CLIENT_ADD = netlink.WireGuardClient(
    public_key="public_key", domain="add", remove=False
)
_WG_CLIENT_DEL = netlink.WireGuardClient(
    public_key="public_key", domain="del", remove=True
)

_WG_PEER_STALE = mock.Mock()
_WG_PEER_STALE.WGPEER_A_PUBLIC_KEY = {"value": b"WGPEER_A_PUBLIC_KEY_STALE"}
_WG_PEER_STALE.WGPEER_A_LAST_HANDSHAKE_TIME = {
    "tv_sec": int((datetime.now() - timedelta(hours=5)).timestamp())
}

_WG_PEER = mock.Mock()
_WG_PEER.WGPEER_A_PUBLIC_KEY = {"value": b"WGPEER_A_PUBLIC_KEY"}
_WG_PEER.WGPEER_A_LAST_HANDSHAKE_TIME = {
    "tv_sec": int((datetime.now() - timedelta(seconds=3)).timestamp())
}


def _get_wg_mock(peer):
    info_mock = mock.Mock()
    info_mock.WGDEVICE_A_PEERS.value = [peer]
    wg_instance = WireGuard()
    wg_info_mock = wg_instance.__enter__.return_value
    wg_info_mock.set.return_value = {"WireGuard": "set"}
    wg_info_mock.info.return_value = [info_mock]
    return wg_info_mock


class NetlinkTest(unittest.TestCase):
    def setUp(self) -> None:
        iproute_instance = IPRoute()
        self.route_info_mock = iproute_instance.__enter__.return_value
        # self.addCleanup(mock.patch.stopall)

    def test_find_stale_wireguard_clients_success_with_non_stale_peer(self):
        """Tests find_stale_wireguard_clients no operation on non-stale peers."""
        wg_info_mock = _get_wg_mock(_WG_PEER)
        self.assertListEqual([], netlink.find_stale_wireguard_clients("some_interface"))

    def test_find_stale_wireguard_clients_success_stale_peer(self):
        """Tests find_stale_wireguard_clients removal of stale peer"""
        wg_info_mock = _get_wg_mock(_WG_PEER_STALE)
        self.assertListEqual(
            ["WGPEER_A_PUBLIC_KEY_STALE"],
            netlink.find_stale_wireguard_clients("some_interface"),
        )

    def test_route_handler_add_success(self):
        """Test route_handler for normal add operation."""
        self.route_info_mock.route.return_value = {"key": "value"}
        self.assertDictEqual({"key": "value"}, netlink.route_handler(_WG_CLIENT_ADD))
        self.route_info_mock.route.assert_called_with(
            "add", dst="fe80::282:6eff:fe9d:ecd3/128", oif=mock.ANY
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
        wg_info_mock = _get_wg_mock(_WG_PEER)
        self.assertDictEqual(
            {"WireGuard": "set"}, netlink.update_wireguard_peer(_WG_CLIENT_ADD)
        )
        wg_info_mock.set.assert_called_with(
            "wg-add",
            peer={
                "public_key": "public_key",
                "persistent_keepalive": 15,
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
            ifindex=mock.ANY,
            lladdr="00:00:00:00:00:00",
            dst="fe80::282:6eff:fe9d:ecd3",
        )

    def test_bridge_fdb_handler_del_success(self):
        """Test bridge_fdb_handler for normal del operation."""
        self.route_info_mock.fdb.return_value = {"key": "value"}
        self.assertEqual({"key": "value"}, netlink.bridge_fdb_handler(_WG_CLIENT_DEL))
        self.route_info_mock.fdb.assert_called_with(
            "del",
            ifindex=mock.ANY,
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
        wg_info_mock = _get_wg_mock(_WG_PEER)
        wg_info_mock.set.return_value = {"WireGuard": "set"}
        self.route_info_mock.fdb.return_value = {"IPRoute": "fdb"}
        self.route_info_mock.route.return_value = {"IPRoute": "route"}
        self.assertEqual(expected, netlink.link_handler(_WG_CLIENT_ADD))
        self.route_info_mock.fdb.assert_called_with(
            "append",
            ifindex=mock.ANY,
            lladdr="00:00:00:00:00:00",
            dst="fe80::282:6eff:fe9d:ecd3",
        )
        self.route_info_mock.route.assert_called_with(
            "add", dst="fe80::282:6eff:fe9d:ecd3/128", oif=mock.ANY
        )
        wg_info_mock.set.assert_called_with(
            "wg-add",
            peer={
                "public_key": "public_key",
                "persistent_keepalive": 15,
                "allowed_ips": ["fe80::282:6eff:fe9d:ecd3/128"],
                "remove": False,
            },
        )

    def test_wg_flush_stale_peers_not_stale_success(self):
        """Tests processing of non-stale WireGuard Peer."""
        wg_info_mock = _get_wg_mock(_WG_PEER)
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
        wg_info_mock = _get_wg_mock(_WG_PEER_STALE)
        wg_info_mock.set.return_value = {"WireGuard": "set"}
        self.assertListEqual(expected, netlink.wg_flush_stale_peers("domain"))
        self.route_info_mock.route.assert_called_with(
            "del", dst="fe80::281:16ff:fe49:395e/128", oif=mock.ANY
        )


if __name__ == "__main__":
    unittest.main()
