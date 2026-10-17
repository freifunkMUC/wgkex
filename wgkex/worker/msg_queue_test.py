"""Tests for msg_queue.py"""

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


if __name__ == "__main__":
    unittest.main()
