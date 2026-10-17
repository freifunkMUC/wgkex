"""Unit tests for drain.py"""

import itertools
import threading
import unittest

from wgkex.common.mqtt import STATUS_DRAINING, STATUS_ONLINE
from wgkex.worker import drain


class DrainStateTest(unittest.TestCase):
    def test_signal_request(self):
        state = drain.DrainState()
        self.assertEqual(STATUS_ONLINE, state.status())
        state.request(True)
        self.assertTrue(state.changed.is_set())
        self.assertEqual(STATUS_DRAINING, state.status())
        state.request(False)
        self.assertEqual(STATUS_ONLINE, state.status())

    def test_file(self):
        files = set()
        state = drain.DrainState("/run/wgkex/drain", exists=lambda f: f in files)
        self.assertFalse(state.is_draining())
        files.add("/run/wgkex/drain")
        self.assertTrue(state.is_draining())
        # SIGUSR2 does not override an existing drain file
        state.request(False)
        self.assertTrue(state.is_draining())
        files.clear()
        self.assertFalse(state.is_draining())

    def test_file_errors(self):
        def exists(_):
            raise OSError("permission denied")

        self.assertFalse(drain.DrainState("/x", exists=exists).is_draining())


class WatchTest(unittest.TestCase):
    def test_publishes_changes_and_retries(self):
        state = drain.DrainState()
        exit_event = threading.Event()
        published = []
        ready = threading.Event()
        calls = threading.Semaphore(0)

        def publish(status):
            calls.release()
            if not ready.is_set():
                return False  # worker not announced yet
            published.append(status)
            return True

        thread = threading.Thread(
            target=drain.watch, args=(exit_event, publish, 0.01, state)
        )
        thread.start()
        try:
            self.assertTrue(calls.acquire(timeout=2))
            ready.set()
            state.request(True)
            for _ in range(200):
                if STATUS_DRAINING in published:
                    break
                calls.acquire(timeout=0.05)
            state.request(False)
            for _ in range(200):
                if published and published[-1] == STATUS_ONLINE:
                    break
                calls.acquire(timeout=0.05)
        finally:
            exit_event.set()
            state.changed.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIn(STATUS_DRAINING, published)
        self.assertEqual(STATUS_ONLINE, published[-1])
        # Unchanged states are not published again
        self.assertTrue(all(a != b for a, b in itertools.pairwise(published)))


if __name__ == "__main__":
    unittest.main()
