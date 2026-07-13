#!/usr/bin/env python3
import json
import threading
from ipaddress import IPv6Network
from queue import Empty, Queue

from wgkex.common import logger
from wgkex.worker.netlink import (
    get_parker_prefixes_for_peer,
    ParkerWireGuardClient,
    PeerMutationError,
    WireGuardClient,
    link_handler,
)
from wgkex.worker.peer_state import PeerMutationCoordinator, peer_mutations


class UniqueQueue(Queue):
    def put(self, item, block=True, timeout=None):
        if item not in self.queue:
            Queue.put(self, item, block, timeout)

    def _init(self, maxsize):
        self.queue = set()

    def _put(self, item):
        self.queue.add(item)

    def _get(self):
        return self.queue.pop()


q = UniqueQueue()


def watch_queue(
    parker: bool,
    stop_event: threading.Event,
    coordinator: PeerMutationCoordinator = peer_mutations,
    parker_prefix_length: int = 63,
    initial_handshake_grace: float = 600,
) -> threading.Thread:
    """Watches the queue for new messages."""
    logger.debug("Starting queue watcher")
    thread = threading.Thread(
        target=pick_from_queue,
        args=(
            parker,
            q,
            stop_event,
            coordinator,
            parker_prefix_length,
            initial_handshake_grace,
        ),
        name="peer-update-queue",
    )
    thread.start()
    return thread


def _process_queue_item(
    item,
    parker: bool,
    coordinator: PeerMutationCoordinator = peer_mutations,
    parker_prefix_length: int = 63,
    initial_handshake_grace: float = 600,
) -> None:
    if parker:
        message = json.loads(item)
        if not isinstance(message, dict):
            raise ValueError("Parker queue item must be an object")
        public_key = message.get("PublicKey")
        range6 = message.get("Range6")
        if not isinstance(public_key, str) or not public_key:
            raise ValueError("Parker queue item has no valid PublicKey")
        try:
            parsed_range6 = IPv6Network(range6)
        except (TypeError, ValueError) as error:
            raise ValueError("Parker queue item has no valid Range6") from error
        if parsed_range6.prefixlen != parker_prefix_length:
            raise ValueError(
                "Parker queue item Range6 prefix length "
                f"must be /{parker_prefix_length}"
            )
        previous_ranges6 = tuple(
            get_parker_prefixes_for_peer("wg-nodes", public_key, parker_prefix_length)
        )
        client = ParkerWireGuardClient(
            public_key=public_key,
            range6=str(parsed_range6),
            keepalive=message.get("Keepalive"),
            remove=False,
            previous_ranges6=previous_ranges6,
        )
        logger.info(
            "Processing queue for key %s with range %s",
            client.public_key,
            client.range6,
        )
    else:
        domain, message = item
        if (
            not isinstance(domain, str)
            or not domain
            or not isinstance(message, str)
            or not message
        ):
            raise ValueError("Legacy queue item has no valid domain or public key")
        logger.debug("Processing queue item %s for domain %s", message, domain)
        client = WireGuardClient(
            public_key=message,
            domain=domain,
            remove=False,
        )
        logger.info(
            "Processing queue for key %s on domain %s with lladdr %s",
            client.public_key,
            domain,
            client.lladdr,
        )
    related_resources = (
        [client.range6, *client.previous_ranges6]
        if isinstance(client, ParkerWireGuardClient)
        else None
    )
    with coordinator.peer_lock(client.public_key, related_resources):
        coordinator.record_provisioned(client.public_key, initial_handshake_grace)
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


def pick_from_queue(
    parker: bool = False,
    work_queue: Queue = q,
    stop_event: threading.Event | None = None,
    coordinator: PeerMutationCoordinator = peer_mutations,
    parker_prefix_length: int = 63,
    initial_handshake_grace: float = 600,
) -> None:
    """Picks a message from the queue and processes it."""
    logger.debug("Starting queue processor")
    while stop_event is None or not stop_event.is_set():
        try:
            item = work_queue.get(timeout=0.1)
        except Empty:
            continue

        try:
            _process_queue_item(
                item,
                parker,
                coordinator,
                parker_prefix_length,
                initial_handshake_grace,
            )
        except Exception as error:
            logger.error(
                "Failed to process queue item %r; continuing with the next item",
                item,
                exc_info=error,
            )
        finally:
            work_queue.task_done()
