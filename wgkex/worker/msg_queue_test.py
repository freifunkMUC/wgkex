import json
import threading
import time
import unittest
from queue import Queue

import mock

from wgkex.worker import msg_queue


class TestMessageQueue(unittest.TestCase):
    @mock.patch.object(msg_queue, "link_handler")
    def test_bad_item_does_not_stop_processing_or_break_accounting(self, link_handler):
        work_queue = Queue()
        stop_event = threading.Event()
        processor = threading.Thread(
            target=msg_queue.pick_from_queue,
            args=(True, work_queue, stop_event),
            daemon=True,
        )
        processor.start()

        work_queue.put("{malformed")
        work_queue.put("[]")
        work_queue.put(
            json.dumps(
                {
                    "PublicKey": "valid-key",
                    "Range6": "2001:db8::/63",
                    "Keepalive": 25,
                }
            )
        )

        deadline = time.monotonic() + 5
        while work_queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)

        stop_event.set()
        processor.join(timeout=1)

        self.assertEqual(work_queue.unfinished_tasks, 0)
        self.assertEqual(link_handler.call_count, 1)
        self.assertEqual(link_handler.call_args.args[0].public_key, "valid-key")

    @mock.patch.object(
        msg_queue, "link_handler", side_effect=[RuntimeError("netlink failed"), {}]
    )
    def test_netlink_failure_does_not_stop_following_item(self, link_handler):
        work_queue = Queue()
        stop_event = threading.Event()
        processor = threading.Thread(
            target=msg_queue.pick_from_queue,
            args=(False, work_queue, stop_event),
            daemon=True,
        )
        processor.start()

        work_queue.put(("domain", "first-key"))
        work_queue.put(("domain", "second-key"))

        deadline = time.monotonic() + 5
        while work_queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)

        stop_event.set()
        processor.join(timeout=1)

        self.assertEqual(work_queue.unfinished_tasks, 0)
        self.assertEqual(link_handler.call_count, 2)
        self.assertEqual(link_handler.call_args.args[0].public_key, "second-key")


if __name__ == "__main__":
    unittest.main()
