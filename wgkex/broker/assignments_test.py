"""Tests for assignments.py"""

import unittest

from wgkex.broker.assignments import Assignment, AssignmentHistory


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class AssignmentHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()

    def test_record_returns_previous(self):
        history = AssignmentHistory(ttl=100, max_entries=10, clock=self.clock)
        self.assertIsNone(history.record("k", "d", "w1"))
        self.clock.now += 10
        self.assertEqual(history.record("k", "d", "w2"), Assignment("d", "w1", 1000))
        self.assertEqual(history.get("k"), Assignment("d", "w2", 1010))

    def test_expiry(self):
        history = AssignmentHistory(ttl=100, max_entries=10, clock=self.clock)
        history.record("k1", "d", "w1")
        self.clock.now += 50
        history.record("k2", "d", "w1")
        self.clock.now += 60
        self.assertIsNone(history.get("k1"))
        self.assertIsNotNone(history.get("k2"))
        self.assertIsNone(history.record("k1", "d", "w2"))
        self.assertEqual(len(history), 2)

    def test_size_limit(self):
        history = AssignmentHistory(ttl=100, max_entries=2, clock=self.clock)
        history.record("k1", "d", "w1")
        history.record("k2", "d", "w1")
        history.record("k3", "d", "w1")
        self.assertEqual(len(history), 2)
        self.assertIsNone(history.get("k1"))
        # Re-recording moves a key to the end
        history.record("k2", "d", "w2")
        history.record("k4", "d", "w1")
        self.assertIsNone(history.get("k3"))
        self.assertIsNotNone(history.get("k2"))

    def test_disabled(self):
        history = AssignmentHistory(ttl=100, max_entries=0, clock=self.clock)
        history.record("k1", "d", "w1")
        self.assertIsNone(history.record("k1", "d", "w2"))
        self.assertEqual(len(history), 0)


if __name__ == "__main__":
    unittest.main()
