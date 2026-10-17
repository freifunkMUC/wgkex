import threading
from queue import Queue
from time import sleep
from typing import NamedTuple

from wgkex.common import logger
from wgkex.worker.netlink import WireGuardClient, link_handler
from wgkex.worker.rebalance import peer_add_tracker


class PeerOp(NamedTuple):
    """A queued peer operation.

    Attributes:
        domain: The domain without prefix, e.g. "welt" for interface wg-welt.
        public_key: The public key of the peer.
        remove: Remove the peer instead of adding it.
    """

    domain: str
    public_key: str
    remove: bool = False


class UniqueQueue(Queue):
    """A queue which drops duplicate items.

    For PeerOp items, adding and removing the same peer cancel each other out: the newest
    operation replaces a still queued one with the opposite action, as the queue is unordered.
    """

    def put(self, item, block=True, timeout=None):
        if isinstance(item, PeerOp):
            opposite = item._replace(remove=not item.remove)
            with self.mutex:
                if opposite in self.queue:
                    self.queue.discard(opposite)
                    self.queue.add(item)
                    return
        if item not in self.queue:
            Queue.put(self, item, block, timeout)

    def _init(self, maxsize):
        self.queue = set()

    def _put(self, item):
        self.queue.add(item)

    def _get(self):
        return self.queue.pop()


q = UniqueQueue()


def watch_queue() -> None:
    """Watches the queue for new messages."""
    logger.debug("Starting queue watcher")
    threading.Thread(target=pick_from_queue, daemon=True).start()


def process_item(item: PeerOp) -> None:
    """Adds or removes a peer."""
    client = WireGuardClient(
        public_key=item.public_key,
        domain=item.domain,
        remove=item.remove,
    )
    if item.remove:
        logger.info(
            f"Removing peer {client.public_key} on domain {item.domain} with lladdr {client.lladdr}"
        )
        peer_add_tracker.forget(client.wg_interface, client.public_key)
        logger.debug(link_handler(client))
        return
    logger.info(
        f"Processing queue for key {client.public_key} on domain {item.domain} with lladdr {client.lladdr}"
    )
    logger.debug(link_handler(client))
    peer_add_tracker.note_added(client.wg_interface, client.public_key)


def pick_from_queue() -> None:
    """Picks a message from the queue and processes it."""
    logger.debug("Starting queue processor")
    while True:
        if not q.empty():
            logger.debug("Queue is not empty current size is %i", q.qsize())
            item = q.get()
            try:
                process_item(item)
            except Exception as e:  # noqa: BLE001
                # Don't crash the queue processor
                if getattr(item, "remove", False):
                    # Expected when the peer was already removed, e.g. by rebalancing
                    logger.info(
                        "Could not remove %s, probably already gone: %s", item, e
                    )
                else:
                    logger.error("Failed to process %s: %s", item, e)
            finally:
                q.task_done()
        else:
            sleep(1)
