"""Tests for msg_queue.py"""

import threading
import time
import unittest
from unittest import mock

from wgkex.worker import msg_queue
from wgkex.worker.msg_queue import PeerOp, UniqueQueue


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

    def test_empty_queue_times_out(self):
        self.assertEqual(0, msg_queue.process_batch(timeout=0.01))

    @mock.patch.object(msg_queue, "process_item")
    def test_batch_limit_and_errors(self, process_mock):
        process_mock.side_effect = [RuntimeError("boom")] + [None] * 10
        for i in range(5):
            self.q.put(PeerOp("d", f"k{i}"))
        self.assertEqual(3, msg_queue.process_batch(max_items=3, timeout=0))
        self.assertEqual(2, msg_queue.process_batch(timeout=0))
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


if __name__ == "__main__":
    unittest.main()
