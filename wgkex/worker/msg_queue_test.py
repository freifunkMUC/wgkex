import json
import threading
import time
import unittest
from queue import Queue

import mock

from wgkex.worker import msg_queue
from wgkex.worker import netlink
from wgkex.worker.peer_state import PeerMutationCoordinator


class MutableClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class TestMessageQueue(unittest.TestCase):
    def setUp(self):
        self.prefixes = mock.patch.object(
            msg_queue, "get_parker_prefixes_for_peer", return_value=[]
        )
        self.get_prefixes = self.prefixes.start()
        self.addCleanup(self.prefixes.stop)

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

    @mock.patch.object(msg_queue, "link_handler")
    def test_accepted_updates_refresh_provisioning_state(self, link_handler):
        clock = MutableClock()
        coordinator = PeerMutationCoordinator(clock=clock)
        msg_queue._process_queue_item(
            json.dumps(
                {
                    "PublicKey": "key",
                    "Range6": "2001:db8::/63",
                    "Keepalive": 25,
                }
            ),
            True,
            coordinator,
        )
        self.assertTrue(coordinator.recently_provisioned("key", 600))
        link_handler.assert_called_once()

    @mock.patch.object(msg_queue, "link_handler")
    def test_parker_update_carries_and_locks_previous_prefixes(self, link_handler):
        self.get_prefixes.return_value = ["2001:db8:1::/63"]
        coordinator = mock.MagicMock()
        coordinator.peer_lock.return_value.__enter__.return_value = None

        msg_queue._process_queue_item(
            json.dumps(
                {
                    "PublicKey": "key",
                    "Range6": "2001:db8:2::/63",
                    "Keepalive": 25,
                }
            ),
            True,
            coordinator,
        )

        client = link_handler.call_args.args[0]
        self.assertEqual(client.previous_ranges6, ("2001:db8:1::/63",))
        coordinator.peer_lock.assert_called_once_with(
            "key", ["2001:db8:2::/63", "2001:db8:1::/63"]
        )

    @mock.patch.object(msg_queue, "link_handler")
    def test_malformed_queue_items_are_rejected(self, link_handler):
        invalid_items = (
            ("[]", True),
            ('{"PublicKey":"","Range6":"2001:db8::/63"}', True),
            ('{"PublicKey":"key","Range6":"invalid"}', True),
            ('{"PublicKey":"key","Range6":"2001:db8::/64"}', True),
            (("", "key"), False),
            (("domain", ""), False),
        )
        for item, parker in invalid_items:
            with self.subTest(item=item), self.assertRaises(ValueError):
                msg_queue._process_queue_item(item, parker)
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
            msg_queue._process_queue_item(
                json.dumps(
                    {
                        "PublicKey": "key",
                        "Range6": "2001:db8:2::/63",
                    }
                ),
                True,
                coordinator,
            )

        self.assertEqual(
            coordinator.pending_parker_routes(),
            ("2001:db8:1::/63",),
        )

    def test_concurrent_update_defers_cleanup_for_same_peer(self):
        clock = MutableClock()
        coordinator = PeerMutationCoordinator(clock=clock)
        update_entered = threading.Event()
        release_update = threading.Event()
        cleanup_results = []
        stale = netlink.StaleWireGuardPeer("key", None, "stale_handshake")

        def update_peer(_):
            update_entered.set()
            release_update.wait(timeout=1)
            return {}

        with (
            mock.patch.object(msg_queue, "link_handler", side_effect=update_peer),
            mock.patch.object(
                netlink, "find_stale_wireguard_clients", return_value=[stale]
            ),
            mock.patch.object(netlink, "link_handler") as delete_peer,
        ):
            update_thread = threading.Thread(
                target=msg_queue._process_queue_item,
                args=(("domain", "key"), False, coordinator),
            )
            update_thread.start()
            self.assertTrue(update_entered.wait(timeout=1))

            cleanup_thread = threading.Thread(
                target=lambda: cleanup_results.extend(
                    netlink.wg_flush_stale_peers(
                        False,
                        "domain",
                        initial_handshake_grace=600,
                        coordinator=coordinator,
                        clock=clock,
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
