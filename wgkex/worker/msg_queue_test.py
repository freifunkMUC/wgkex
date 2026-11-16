"""Tests for msg_queue.py"""

import json
import threading
import time
import unittest
from unittest import mock

from wgkex.worker import msg_queue, netlink, observability
from wgkex.worker.msg_queue import PeerOp, UniqueQueue
from wgkex.worker.peer_state import PeerMutationCoordinator


class MutableClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


def _peer_ops(op: str, result: str) -> float:
    value = observability.REGISTRY.get_sample_value(
        "wgkex_worker_peer_operations_total", {"op": op, "result": result}
    )
    return value or 0.0


class UniqueQueueTest(unittest.TestCase):
    def test_dedupe(self):
        q = UniqueQueue()
        q.put(PeerOp("d", "k"))
        q.put(PeerOp("d", "k"))
        q.put(("d", "other"))
        q.put(("d", "other"))
        self.assertEqual(q.qsize(), 2)

    def test_latest_operation_wins(self):
        q = UniqueQueue()
        q.put(PeerOp("d", "k"))
        q.put(PeerOp("d", "k", remove=True))
        self.assertEqual(q.qsize(), 1)
        self.assertEqual(q.get_nowait(), PeerOp("d", "k", remove=True))
        q.put(PeerOp("d", "k", remove=True))
        q.put(PeerOp("d", "k"))
        self.assertEqual(q.qsize(), 1)
        self.assertEqual(q.get_nowait(), PeerOp("d", "k"))


class ProcessItemTest(unittest.TestCase):
    @mock.patch.object(msg_queue, "link_handler")
    def test_add_and_remove(self, link_mock):
        tracker = msg_queue.peer_add_tracker
        msg_queue.process_item(PeerOp("dom", "KEY"))
        client = link_mock.call_args.args[0]
        self.assertFalse(client.remove)
        self.assertEqual(client.wg_interface, "wg-dom")
        self.assertIsNotNone(tracker.added_at("wg-dom", "KEY"))

        msg_queue.process_item(PeerOp("dom", "KEY", remove=True))
        client = link_mock.call_args.args[0]
        self.assertTrue(client.remove)
        self.assertIsNone(tracker.added_at("wg-dom", "KEY"))


class ProcessBatchTest(unittest.TestCase):
    def setUp(self):
        self.q = UniqueQueue()
        patcher = mock.patch.object(msg_queue, "q", self.q)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_queue_depth_metric(self):
        self.q.put(PeerOp("d", "k1"))
        self.q.put(PeerOp("d", "k2", remove=True))
        self.assertEqual(
            2, observability.REGISTRY.get_sample_value("wgkex_worker_queue_depth")
        )

    def test_empty_queue_times_out(self):
        self.assertEqual(0, msg_queue.process_batch(timeout=0.01))

    @mock.patch.object(msg_queue, "process_item")
    def test_batch_limit_and_errors(self, process_mock):
        process_mock.side_effect = [RuntimeError("boom")] + [None] * 10
        for i in range(5):
            self.q.put(PeerOp("d", f"k{i}"))
        errors_before = _peer_ops("add", "error")
        ok_before = _peer_ops("add", "ok")
        self.assertEqual(3, msg_queue.process_batch(max_items=3, timeout=0))
        self.assertEqual(2, msg_queue.process_batch(timeout=0))
        self.assertEqual(errors_before + 1, _peer_ops("add", "error"))
        self.assertEqual(ok_before + 4, _peer_ops("add", "ok"))
        self.assertEqual(5, process_mock.call_count)
        # task_done was called for every item, even the failed one
        self.q.join()

    @mock.patch.object(msg_queue, "link_handler")
    def test_throughput(self, link_mock):
        """Queued operations are processed without per-item polling delays."""
        count = 5000
        for i in range(count):
            self.q.put(PeerOp("dom", f"KEY{i}"))
            self.q.put(PeerOp("dom", f"KEY{i}"))  # duplicates are dropped
        self.assertEqual(count, self.q.qsize())
        start = time.monotonic()
        processed = 0
        while processed < count:
            processed += msg_queue.process_batch(timeout=0)
        elapsed = time.monotonic() - start
        self.assertEqual(count, link_mock.call_count)
        # The old implementation slept 1s per poll; this should take well under a second
        self.assertLess(elapsed, 5)

    def test_blocking_get_wakes_up(self):
        """A waiting processor picks up an item as soon as it is queued."""
        results = []
        with mock.patch.object(msg_queue, "process_item", results.append):
            thread = threading.Thread(target=lambda: msg_queue.process_batch(timeout=5))
            thread.start()
            time.sleep(0.05)
            self.q.put(PeerOp("d", "k"))
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual([PeerOp("d", "k")], results)


class ParkerProcessBatchTest(unittest.TestCase):
    def setUp(self):
        self.q = UniqueQueue()
        patcher = mock.patch.object(msg_queue, "q", self.q)
        patcher.start()
        self.addCleanup(patcher.stop)
        prefixes = mock.patch.object(
            msg_queue, "get_parker_prefixes_for_peer", return_value=[]
        )
        prefixes.start()
        self.addCleanup(prefixes.stop)

    @mock.patch.object(msg_queue, "link_handler")
    def test_bad_item_does_not_stop_processing_or_break_accounting(self, link_handler):
        self.q.put("{malformed")
        self.q.put("[]")
        self.q.put(
            json.dumps(
                {
                    "PublicKey": "valid-key",
                    "Range6": "2001:db8::/63",
                    "Keepalive": 25,
                }
            )
        )

        self.assertEqual(3, msg_queue.process_batch(timeout=0))

        self.assertEqual(self.q.unfinished_tasks, 0)
        self.assertEqual(link_handler.call_count, 1)
        self.assertEqual(link_handler.call_args.args[0].public_key, "valid-key")

    @mock.patch.object(
        msg_queue, "link_handler", side_effect=[RuntimeError("netlink failed"), {}]
    )
    def test_netlink_failure_does_not_stop_following_item(self, link_handler):
        self.q.put(PeerOp("domain", "first-key"))
        self.q.put(PeerOp("domain", "second-key"))

        self.assertEqual(2, msg_queue.process_batch(timeout=0))

        self.assertEqual(self.q.unfinished_tasks, 0)
        self.assertEqual(link_handler.call_count, 2)
        self.assertEqual(link_handler.call_args.args[0].public_key, "second-key")

    @mock.patch.object(msg_queue, "parker_prefix_owned_by_other_peer")
    @mock.patch.object(msg_queue, "parker_link_handler")
    def test_parker_removal_is_counted_as_remove(self, parker_link_handler, owned):
        owned.return_value = False
        before = _peer_ops("remove", "ok")
        self.q.put(
            json.dumps({"PublicKey": "key", "Range6": "2001:db8::/63", "Remove": True})
        )
        self.assertEqual(1, msg_queue.process_batch(timeout=0))
        self.assertEqual(before + 1, _peer_ops("remove", "ok"))
        parker_link_handler.assert_called_once()


class ParkerProcessItemTest(unittest.TestCase):
    def setUp(self):
        prefixes = mock.patch.object(
            msg_queue, "get_parker_prefixes_for_peer", return_value=[]
        )
        self.get_prefixes = prefixes.start()
        self.addCleanup(prefixes.stop)

    def _options(self, coordinator):
        return msg_queue.ParkerOptions(
            prefix_length=63, initial_handshake_grace=600, coordinator=coordinator
        )

    @mock.patch.object(msg_queue, "link_handler")
    def test_accepted_updates_refresh_provisioning_state(self, link_handler):
        coordinator = PeerMutationCoordinator(clock=MutableClock())
        msg_queue.process_parker_item(
            json.dumps(
                {"PublicKey": "key", "Range6": "2001:db8::/63", "Keepalive": 25}
            ),
            self._options(coordinator),
        )
        self.assertTrue(coordinator.recently_provisioned("key", 600))
        link_handler.assert_called_once()
        self.assertEqual(link_handler.call_args.args[0].keepalive, 25)

    @mock.patch.object(msg_queue, "link_handler")
    def test_parker_update_carries_and_locks_previous_prefixes(self, link_handler):
        self.get_prefixes.return_value = ["2001:db8:1::/63"]
        coordinator = mock.MagicMock()
        coordinator.peer_lock.return_value.__enter__.return_value = None

        msg_queue.process_parker_item(
            json.dumps({"PublicKey": "key", "Range6": "2001:db8:2::/63"}),
            self._options(coordinator),
        )

        client = link_handler.call_args.args[0]
        self.assertEqual(client.previous_ranges6, ("2001:db8:1::/63",))
        coordinator.peer_lock.assert_called_once_with(
            "key", ["2001:db8:2::/63", "2001:db8:1::/63"]
        )

    @mock.patch.object(
        msg_queue, "parker_prefix_owned_by_other_peer", side_effect=[False, True]
    )
    @mock.patch.object(msg_queue, "parker_link_handler")
    def test_parker_remove_message_removes_peer_and_forgets(
        self, parker_link_handler, owned
    ):
        coordinator = mock.MagicMock()
        coordinator.peer_lock.return_value.__enter__.return_value = None
        item = json.dumps(
            {"PublicKey": "key", "Range6": "2001:db8::/63", "Remove": True}
        )

        msg_queue.process_parker_item(item, self._options(coordinator))

        client = parker_link_handler.call_args.args[0]
        self.assertTrue(client.remove)
        parker_link_handler.assert_called_once_with(client, preserve_parker_route=False)
        coordinator.record_provisioned.assert_not_called()
        coordinator.forget.assert_called_once_with("key")

        # A prefix that has moved to another peer must be preserved.
        msg_queue.process_parker_item(item, self._options(coordinator))
        self.assertEqual(
            parker_link_handler.call_args.kwargs, {"preserve_parker_route": True}
        )
        owned.assert_called_with("wg-nodes", "2001:db8::/63", "key", 63)

    @mock.patch.object(msg_queue, "link_handler")
    def test_malformed_queue_items_are_rejected(self, link_handler):
        for item in (
            "[]",
            '{"PublicKey":"","Range6":"2001:db8::/63"}',
            '{"PublicKey":"key","Range6":"invalid"}',
            '{"PublicKey":"key","Range6":"2001:db8::/64"}',
        ):
            with self.subTest(item=item), self.assertRaises(ValueError):
                msg_queue.process_parker_item(
                    item, self._options(PeerMutationCoordinator())
                )
        link_handler.assert_not_called()

    @mock.patch.object(msg_queue, "link_handler")
    def test_failed_old_route_is_queued_for_cleanup(self, link_handler):
        client = netlink.ParkerWireGuardClient("key", "2001:db8:2::/63", False)
        link_handler.side_effect = netlink.PeerMutationError(
            "old_route:2001:db8:1::/63",
            client,
            {
                "wireguard": netlink.OperationOutcome("updated"),
                "route": netlink.OperationOutcome("updated"),
            },
            RuntimeError("route failed"),
        )
        coordinator = PeerMutationCoordinator()

        with self.assertRaises(netlink.PeerMutationError):
            msg_queue.process_parker_item(
                json.dumps({"PublicKey": "key", "Range6": "2001:db8:2::/63"}),
                self._options(coordinator),
            )

        self.assertEqual(coordinator.pending_parker_routes(), ("2001:db8:1::/63",))

    def test_concurrent_update_defers_cleanup_for_same_peer(self):
        coordinator = PeerMutationCoordinator(clock=MutableClock())
        update_entered = threading.Event()
        release_update = threading.Event()
        cleanup_results = []
        stale = netlink.StaleWireGuardPeer("key", "2001:db8::/63", "stale_handshake")

        def update_peer(_):
            update_entered.set()
            release_update.wait(timeout=1)
            return {}

        with (
            mock.patch.object(msg_queue, "link_handler", side_effect=update_peer),
            mock.patch.object(netlink, "find_stale_parker_peers", return_value=[stale]),
            mock.patch.object(netlink, "parker_link_handler") as delete_peer,
        ):
            update_thread = threading.Thread(
                target=msg_queue.process_parker_item,
                args=(
                    json.dumps({"PublicKey": "key", "Range6": "2001:db8::/63"}),
                    self._options(coordinator),
                ),
            )
            update_thread.start()
            self.assertTrue(update_entered.wait(timeout=1))

            cleanup_thread = threading.Thread(
                target=lambda: cleanup_results.extend(
                    netlink.wg_flush_stale_peers(
                        True,
                        initial_handshake_grace=600,
                        coordinator=coordinator,
                    )
                )
            )
            cleanup_thread.start()
            time.sleep(0.05)
            delete_peer.assert_not_called()
            release_update.set()
            update_thread.join(timeout=1)
            cleanup_thread.join(timeout=1)

        self.assertEqual(cleanup_results[0].status, "deferred")
        delete_peer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
