"""Unit tests for gateway_keys.py"""

import unittest

from wgkex.worker.gateway_keys import GatewayKeys


class GatewayKeysTest(unittest.TestCase):
    def setUp(self) -> None:
        self.keys = GatewayKeys()

    def test_learns_keys_and_syncs_on_own_data(self):
        self.keys.handle_worker_data("gw05", b'{"PublicKey": "GW05="}', "gw04")
        self.assertIn("GW05=", self.keys)
        self.assertNotIn("OTHER=", self.keys)
        self.assertFalse(self.keys.synced)
        self.keys.handle_worker_data("gw04", '{"PublicKey": "GW04="}', "gw04")
        self.assertIn("GW04=", self.keys)
        self.assertTrue(self.keys.synced)

    def test_ignores_invalid_data(self):
        for payload in (b"", b"not json", b"[]", b"{}", b'{"PublicKey": 1}'):
            self.keys.handle_worker_data("gw04", payload, "gw04")
        self.assertFalse(self.keys.synced)

    def test_keys_are_never_forgotten(self):
        self.keys.handle_worker_data("gw05", b'{"PublicKey": "OLD="}', "gw04")
        self.keys.handle_worker_data("gw05", b'{"PublicKey": "NEW="}', "gw04")
        self.keys.handle_worker_data("gw05", b"", "gw04")
        self.assertIn("OLD=", self.keys)
        self.assertIn("NEW=", self.keys)

    def test_reset(self):
        self.keys.handle_worker_data("gw04", b'{"PublicKey": "GW04="}', "gw04")
        self.keys.reset()
        self.assertNotIn("GW04=", self.keys)
        self.assertFalse(self.keys.synced)


if __name__ == "__main__":
    unittest.main()
