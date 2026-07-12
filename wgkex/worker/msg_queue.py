#!/usr/bin/env python3
import json
import threading
from queue import Empty, Queue

from wgkex.common import logger
from wgkex.worker.netlink import (
    ParkerWireGuardClient,
    WireGuardClient,
    link_handler,
)


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


def watch_queue(parker: bool = False) -> None:
    """Watches the queue for new messages."""
    logger.debug("Starting queue watcher")
    threading.Thread(target=pick_from_queue, args=[parker], daemon=True).start()


def _process_queue_item(item, parker: bool) -> None:
    if parker:
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
    else:
        domain, message = item
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
    logger.debug(link_handler(client))


def pick_from_queue(
    parker: bool = False,
    work_queue: Queue = q,
    stop_event: threading.Event | None = None,
) -> None:
    """Picks a message from the queue and processes it."""
    logger.debug("Starting queue processor")
    while stop_event is None or not stop_event.is_set():
        try:
            item = work_queue.get(timeout=0.1)
        except Empty:
            continue

        try:
            _process_queue_item(item, parker)
        except Exception as error:
            logger.error(
                "Failed to process queue item %r; continuing with the next item",
                item,
                exc_info=error,
            )
        finally:
            work_queue.task_done()
