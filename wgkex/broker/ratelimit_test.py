"""Tests for the key exchange rate limiter."""

import ipaddress
import unittest

from wgkex.broker.ratelimit import RateLimiter, client_address


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class RateLimiterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()

    def limiter(self, **kwargs) -> RateLimiter:
        args = {
            "per_client_requests": 3,
            "per_client_period": 60,
            "global_requests": 0,
            "global_period": 60,
            "clock": self.clock,
        }
        args.update(kwargs)
        return RateLimiter(**args)

    def test_per_client_burst_and_refill(self):
        rl = self.limiter()
        self.assertEqual(
            [rl.allow("192.0.2.1")[0] for _ in range(4)], [True] * 3 + [False]
        )
        self.assertEqual(rl.allow("192.0.2.1"), (False, "client"))
        self.assertTrue(rl.allow("192.0.2.2")[0])
        self.clock.now += 20  # one token per 20s
        self.assertTrue(rl.allow("192.0.2.1")[0])
        self.assertFalse(rl.allow("192.0.2.1")[0])

    def test_ipv6_prefix_aggregation(self):
        rl = self.limiter(per_client_requests=1, ipv6_prefix_length=64)
        self.assertTrue(rl.allow("2001:db8:1:2::1")[0])
        self.assertFalse(rl.allow("2001:db8:1:2::ffff")[0])
        self.assertTrue(rl.allow("2001:db8:1:3::1")[0])
        self.assertEqual(rl.client_id("::ffff:192.0.2.1"), "192.0.2.1")
        self.assertEqual(rl.client_id("2001:db8:1:2::1"), "2001:db8:1:2::/64")
        self.assertEqual(rl.client_id(None), "")
        self.assertEqual(rl.client_id("not-an-ip"), "not-an-ip")

    def test_global_limit_refunds_client_token(self):
        rl = self.limiter(per_client_requests=2, global_requests=1)
        self.assertTrue(rl.allow("192.0.2.1")[0])
        self.assertEqual(rl.allow("192.0.2.2"), (False, "global"))
        self.clock.now += 60
        # 192.0.2.2 did not lose its token to the globally limited request
        self.assertTrue(rl.allow("192.0.2.2")[0])
        self.assertFalse(rl.allow("192.0.2.2")[0])

    def test_disabled(self):
        rl = self.limiter(per_client_requests=0)
        self.assertTrue(all(rl.allow("192.0.2.1")[0] for _ in range(1000)))
        self.assertEqual(rl.tracked_clients(), 0)

    def test_max_clients_and_cleanup(self):
        rl = self.limiter(max_clients=10)
        for i in range(50):
            rl.allow(f"192.0.2.{i}")
        self.assertEqual(rl.tracked_clients(), 10)
        self.clock.now += 120
        rl.allow("198.51.100.1")
        self.assertEqual(rl.tracked_clients(), 1)

    def test_client_address_without_trusted_proxies(self):
        """Without trusted proxies the header is ignored, it can be spoofed by anyone."""
        for trusted in (0, []):
            self.assertEqual(
                client_address("198.51.100.7", "192.0.2.1", trusted), "198.51.100.7"
            )
        self.assertEqual(client_address("10.0.0.1", None, 1), "10.0.0.1")

    def test_client_address_hop_count(self):
        # Single proxy: the client may have sent a spoofed entry, only the rightmost is trusted
        self.assertEqual(client_address("10.0.0.1", "192.0.2.1", 1), "192.0.2.1")
        self.assertEqual(
            client_address("10.0.0.1", "spoofed, 192.0.2.1", 1), "192.0.2.1"
        )
        # Two proxies
        self.assertEqual(
            client_address("10.0.0.2", "spoofed, 192.0.2.1, 10.0.0.1", 2), "192.0.2.1"
        )
        # Fewer entries than proxies: the header can't be trusted
        self.assertEqual(client_address("10.0.0.1", "192.0.2.1", 3), "10.0.0.1")

    def test_client_address_trusted_networks(self):
        trusted = [
            ipaddress.ip_network("10.0.0.0/24"),
            ipaddress.ip_network("2001:db8::/64"),
        ]
        # Spoofed header from an untrusted peer is ignored
        self.assertEqual(
            client_address("198.51.100.7", "192.0.2.1", trusted), "198.51.100.7"
        )
        # Single hop
        self.assertEqual(client_address("10.0.0.1", "192.0.2.1", trusted), "192.0.2.1")
        self.assertEqual(
            client_address("2001:db8::1", "2001:db8:1::5", trusted), "2001:db8:1::5"
        )
        self.assertEqual(
            client_address("::ffff:10.0.0.1", "192.0.2.1", trusted), "192.0.2.1"
        )
        # Multiple hops, spoofed entries left of the client are ignored
        self.assertEqual(
            client_address("10.0.0.1", "spoofed, 192.0.2.1, 10.0.0.2", trusted),
            "192.0.2.1",
        )
        self.assertEqual(
            client_address("10.0.0.1", "203.0.113.9, 192.0.2.1, 10.0.0.2", trusted),
            "192.0.2.1",
        )
        # Only proxies in the header: the leftmost one is the best guess
        self.assertEqual(client_address("10.0.0.1", "10.0.0.3", trusted), "10.0.0.3")
        # Garbage appended right of the proxies: stop at the last verified address
        self.assertEqual(
            client_address("10.0.0.1", "192.0.2.1, garbage", trusted), "10.0.0.1"
        )
        self.assertEqual(client_address("10.0.0.1", " , ", trusted), "10.0.0.1")


if __name__ == "__main__":
    unittest.main()
