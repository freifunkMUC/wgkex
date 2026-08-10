import json
import threading
import time
from dataclasses import dataclass
from ipaddress import IPv6Network
from queue import Empty, Queue
from typing import NamedTuple

from wgkex.common import logger
from wgkex.worker.netlink import (
    ParkerWireGuardClient,
    PeerMutationError,
    WireGuardClient,
    get_parker_prefixes_for_peer,
    link_handler,
    parker_link_handler,
    parker_prefix_owned_by_other_peer,
)
from wgkex.worker.observability import PEER_OPS, QUEUE_DEPTH
from wgkex.worker.peer_state import PeerMutationCoordinator, peer_mutations
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


@dataclass
class ParkerOptions:
    """Settings for processing Parker queue items.

    Attributes:
        prefix_length: The prefix length of the node prefixes (Range6) assigned by the broker.
        initial_handshake_grace: Seconds a new peer is protected from stale peer cleanup.
        coordinator: Serializes queue updates and cleanup of the same peer or prefix.
    """

    prefix_length: int = 63
    initial_handshake_grace: float = 600
    coordinator: PeerMutationCoordinator = peer_mutations


parker_options = ParkerOptions()


def watch_queue(
    parker: bool = False,
    *,
    parker_prefix_length: int | None = None,
    initial_handshake_grace: float | None = None,
    coordinator: PeerMutationCoordinator | None = None,
) -> threading.Thread:
    """Watches the queue for new messages."""
    if parker_prefix_length is not None:
        parker_options.prefix_length = parker_prefix_length
    if initial_handshake_grace is not None:
        parker_options.initial_handshake_grace = initial_handshake_grace
    if coordinator is not None:
        parker_options.coordinator = coordinator
    logger.debug("Starting queue watcher")
    thread = threading.Thread(
        target=pick_from_queue, name="peer-update-queue", daemon=True
    )
    thread.start()
    return thread


def parse_parker_item(
    item: str, prefix_length: int
) -> tuple[str, str, int | None, bool]:
    """Validates a Parker JSON queue item.

    Returns:
        The public key, the normalized Range6, the keepalive and whether to remove the peer.

    Raises:
        ValueError: If the item is malformed.
    """
    message = json.loads(item)
    if not isinstance(message, dict):
        raise ValueError("Parker queue item must be an object")
    public_key = message.get("PublicKey")
    if not isinstance(public_key, str) or not public_key:
        raise ValueError("Parker queue item has no valid PublicKey")
    try:
        range6 = IPv6Network(message.get("Range6"))
    except (TypeError, ValueError) as error:
        raise ValueError("Parker queue item has no valid Range6") from error
    if range6.prefixlen != prefix_length:
        raise ValueError(
            f"Parker queue item Range6 prefix length must be /{prefix_length}"
        )
    return public_key, str(range6), message.get("Keepalive"), bool(message.get("Remove"))


def process_parker_item(item: str, options: ParkerOptions | None = None) -> None:
    """Adds, moves or removes a Parker peer from a JSON queue item.

    Routes of prefixes previously assigned to the peer are removed after the new state is
    installed. Old route deletions which fail are retried by the stale peer cleanup.
    """
    options = options or parker_options
    public_key, range6, keepalive, remove = parse_parker_item(
        item, options.prefix_length
    )
    coordinator = options.coordinator
    previous_ranges6 = tuple(
        get_parker_prefixes_for_peer("wg-nodes", public_key, options.prefix_length)
    )
    client = ParkerWireGuardClient(
        public_key=public_key,
        range6=range6,
        keepalive=keepalive,
        remove=remove,
        previous_ranges6=previous_ranges6,
    )
    logger.info(
        "Processing queue %s for key %s with range %s",
        "removal" if client.remove else "update",
        client.public_key,
        client.range6,
    )
    with coordinator.peer_lock(client.public_key, [range6, *previous_ranges6]):
        if client.remove:
            # The broker de-selected this worker for the node. Like the stale peer cleanup: keep
            # the route if another peer owns the prefix by now, and forget the peer so a later
            # re-add gets a fresh grace. Provisioning state is not refreshed for removals.
            preserve = parker_prefix_owned_by_other_peer(
                "wg-nodes", client.range6, client.public_key, options.prefix_length
            )
            result = parker_link_handler(client, preserve_parker_route=preserve)
            coordinator.forget(client.public_key)
            logger.debug(result)
            return
        coordinator.record_provisioned(
            client.public_key, options.initial_handshake_grace
        )
        try:
            result = link_handler(client)
        except PeerMutationError as error:
            if error.operation == "wireguard" and not error.operations:
                coordinator.forget(client.public_key)
            if error.operation.startswith("old_route:"):
                coordinator.record_pending_parker_route(
                    error.operation.removeprefix("old_route:")
                )
            raise
        logger.debug(result)


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
