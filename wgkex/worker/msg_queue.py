import json
import threading
import time
from queue import Empty, Queue
from typing import NamedTuple

from wgkex.common import logger
from wgkex.worker.netlink import (
    ParkerWireGuardClient,
    WireGuardClient,
    link_handler,
)
from wgkex.worker.observability import PEER_OPS, QUEUE_DEPTH
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
QUEUE_DEPTH.set_function(lambda: q.qsize())

# Maximum number of queued operations processed at once
_MAX_BATCH = 256


def watch_queue(parker: bool = False) -> None:
    """Watches the queue for new messages."""
    logger.debug("Starting queue watcher")
    threading.Thread(target=pick_from_queue, daemon=True).start()


def process_parker_item(item: str) -> None:
    """Adds a Parker peer from a JSON queue item."""
    message = json.loads(item)
    client = ParkerWireGuardClient(
        # TODO use shared data class
        public_key=message.get("PublicKey"),
        range6=message.get("Range6"),
        keepalive=message.get("Keepalive"),
        remove=False,
    )
    logger.info(
        "Processing queue for key %s with range %s",
        client.public_key,
        client.range6,
    )
    logger.debug(link_handler(client))


def process_item(item: PeerOp | str) -> None:
    """Adds or removes a peer."""
    if isinstance(item, str):
        process_parker_item(item)
        return
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
    """Processes queued peer operations, forever."""
    logger.debug("Starting queue processor")
    while True:
        process_batch()


def process_batch(max_items: int = _MAX_BATCH, timeout: float | None = None) -> int:
    """Waits for queued peer operations and processes all of them (up to max_items).

    Arguments:
        max_items: Maximum number of operations to take from the queue at once.
        timeout: Seconds to wait for the first operation, None waits forever.

    Returns:
        The number of processed operations.
    """
    try:
        batch = [q.get(timeout=timeout)]
    except Empty:
        return 0
    while len(batch) < max_items:
        try:
            batch.append(q.get_nowait())
        except Empty:
            break
    start = time.monotonic()
    # Sorted by interface, so the cached netlink state of an interface is reused back to back
    for item in sorted(batch, key=_sort_key):
        op = "remove" if getattr(item, "remove", False) else "add"
        try:
            process_item(item)
            PEER_OPS.labels(op, "ok").inc()
        except Exception as e:  # noqa: BLE001
            PEER_OPS.labels(op, "error").inc()
            # Don't crash the queue processor
            if getattr(item, "remove", False):
                # Expected when the peer was already removed, e.g. by rebalancing
                logger.info("Could not remove %s, probably already gone: %s", item, e)
            else:
                logger.error("Failed to process %s: %s", item, e)
        finally:
            q.task_done()
    logger.debug(
        "Processed %d peer operations in %.1f ms, %d queued",
        len(batch),
        (time.monotonic() - start) * 1000,
        q.qsize(),
    )
    return len(batch)


def _sort_key(item) -> tuple[str, str]:
    return (str(item[0]), str(item[1]))
