"""Tests for bounded peer mutation state."""

import threading
import unittest

from wgkex.worker.peer_state import PeerMutationCoordinator


class MutableClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class PeerMutationCoordinatorTest(unittest.TestCase):
    def test_provisioning_and_restart_grace_expire(self):
        clock = MutableClock()
        coordinator = PeerMutationCoordinator(clock=clock)
        self.assertTrue(coordinator.defer_never_handshaked("unknown", 10))
        coordinator.record_provisioned("known", 10)

        clock.now = 9
        self.assertTrue(coordinator.recently_provisioned("known", 10))
        clock.now = 10
        self.assertFalse(coordinator.recently_provisioned("known", 10))
        self.assertFalse(coordinator.defer_never_handshaked("unknown", 10))

    def test_tracker_capacity_is_bounded_and_explicit(self):
        clock = MutableClock()
        coordinator = PeerMutationCoordinator(clock=clock, max_provisioned_peers=1)
        coordinator.record_provisioned("first", 10)
        with self.assertRaisesRegex(RuntimeError, "at capacity"):
            coordinator.record_provisioned("second", 10)
        self.assertTrue(coordinator.recently_provisioned("first", 10))
        clock.now = 10
        coordinator.record_provisioned("second", 10)
        self.assertTrue(coordinator.recently_provisioned("second", 10))

    def test_pending_parker_routes_are_bounded_and_removable(self):
        coordinator = PeerMutationCoordinator(max_provisioned_peers=1)
        coordinator.record_pending_parker_route("2001:db8:1::/63")
        coordinator.record_pending_parker_route("2001:db8:1::/63")
        with self.assertRaisesRegex(RuntimeError, "at capacity"):
            coordinator.record_pending_parker_route("2001:db8:2::/63")
        self.assertEqual(coordinator.pending_parker_routes(), ("2001:db8:1::/63",))
        coordinator.forget_pending_parker_route("2001:db8:1::/63")
        self.assertEqual(coordinator.pending_parker_routes(), ())

    def test_same_peer_serializes_while_different_peers_do_not(self):
        coordinator = PeerMutationCoordinator(lock_stripes=257)
        entered = threading.Event()
        release = threading.Event()
        same_peer_entered = threading.Event()

        def hold_lock():
            with coordinator.peer_lock("peer"):
                entered.set()
                release.wait()

        def wait_for_same_peer():
            with coordinator.peer_lock("peer"):
                same_peer_entered.set()

        holder = threading.Thread(target=hold_lock)
        waiter = threading.Thread(target=wait_for_same_peer)
        holder.start()
        entered.wait(timeout=1)
        waiter.start()
        self.assertFalse(same_peer_entered.wait(timeout=0.05))
        with coordinator.peer_lock("other-peer"):
            pass
        release.set()
        holder.join(timeout=1)
        waiter.join(timeout=1)
        self.assertTrue(same_peer_entered.is_set())

    def test_related_resource_serializes_different_peers(self):
        coordinator = PeerMutationCoordinator(lock_stripes=257)
        entered = threading.Event()
        release = threading.Event()
        waiter_entered = threading.Event()

        def hold_prefix():
            with coordinator.peer_lock("old-key", "2001:db8::/63"):
                entered.set()
                release.wait()

        def wait_for_prefix():
            with coordinator.peer_lock("new-key", "2001:db8::/63"):
                waiter_entered.set()

        holder = threading.Thread(target=hold_prefix)
        waiter = threading.Thread(target=wait_for_prefix)
        holder.start()
        entered.wait(timeout=1)
        waiter.start()
        self.assertFalse(waiter_entered.wait(timeout=0.05))
        release.set()
        holder.join(timeout=1)
        waiter.join(timeout=1)
        self.assertTrue(waiter_entered.is_set())


if __name__ == "__main__":
    unittest.main()
