"""Tests for the key exchange rate limiter."""

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

    def test_client_address(self):
        self.assertEqual(client_address("10.0.0.1", "192.0.2.1", 0), "10.0.0.1")
        self.assertEqual(client_address("10.0.0.1", None, 1), "10.0.0.1")
        self.assertEqual(
            client_address("10.0.0.1", "spoofed, 192.0.2.1", 1), "192.0.2.1"
        )
        self.assertEqual(
            client_address("10.0.0.1", "spoofed, 192.0.2.1, 10.0.0.2", 2), "192.0.2.1"
        )
        self.assertEqual(client_address("10.0.0.1", "192.0.2.1", 3), "192.0.2.1")


if __name__ == "__main__":
    unittest.main()
