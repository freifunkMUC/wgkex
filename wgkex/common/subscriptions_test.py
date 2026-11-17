"""Unit tests for subscriptions.py."""

import unittest
from unittest import mock

from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from wgkex.common import subscriptions


class SubscriptionGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = mock.MagicMock()
        self.client.subscribe.side_effect = [(0, 1), (0, 2), (0, 3)]
        self.guard = subscriptions.SubscriptionGuard(retry_delay=0)

    def _wait_for_timers(self) -> None:
        import threading

        for t in threading.enumerate():
            if isinstance(t, threading.Timer):
                t.join(timeout=2)

    def test_failed_suback_resubscribes(self) -> None:
        self.guard.subscribe(self.client, "a/#", qos=1)
        self.guard.on_subscribe(self.client, None, 1, (0x80,))
        self._wait_for_timers()
        self.assertEqual(
            self.client.subscribe.call_args_list,
            [mock.call("a/#", qos=1), mock.call("a/#", qos=1)],
        )

    def test_retries_until_granted(self) -> None:
        self.guard.subscribe(self.client, "a/#")
        self.guard.on_subscribe(self.client, None, 1, (128,))
        self._wait_for_timers()
        self.guard.on_subscribe(self.client, None, 2, (0,))
        self._wait_for_timers()
        self.assertEqual(self.client.subscribe.call_count, 2)

    def test_successful_suback_does_not_resubscribe(self) -> None:
        self.guard.subscribe(self.client, "a/#", qos=1)
        self.guard.on_subscribe(self.client, None, 1, (1,))
        self._wait_for_timers()
        self.client.subscribe.assert_called_once_with("a/#", qos=1)

    def test_reason_code_failure_resubscribes(self) -> None:
        self.guard.subscribe(self.client, "a/#", qos=1)
        self.guard.on_subscribe_v2(
            self.client,
            None,
            1,
            [ReasonCode(PacketTypes.SUBACK, "Not authorized")],
            None,
        )
        self._wait_for_timers()
        self.assertEqual(self.client.subscribe.call_count, 2)

    def test_untracked_mid_is_ignored(self) -> None:
        self.guard.on_subscribe(self.client, None, 42, (0x80,))
        self._wait_for_timers()
        self.client.subscribe.assert_not_called()

    def test_failed_subscribe_call_is_not_tracked(self) -> None:
        self.client.subscribe.side_effect = [(4, 1)]
        self.guard.subscribe(self.client, "a/#")
        self.guard.on_subscribe(self.client, None, 1, (0x80,))
        self._wait_for_timers()
        self.client.subscribe.assert_called_once()


if __name__ == "__main__":
    unittest.main()
