#!/usr/bin/env python3
import threading
from queue import Queue
from wgkex.common import logger
from wgkex.worker.netlink import link_handler
from wgkex.worker.netlink import WireGuardClient


class UniqueQueue(Queue):
    def put(self, item, block=True, timeout=None):
        if item not in self.queue:  # fix join bug
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


def pick_from_queue() -> None:
    """Picks a message from the queue and processes it."""
    logger.debug("Starting queue processor")
    while True:
        if not q.empty():
            logger.debug("Queue is not empty current size is %i", q.qsize())
            domain, message = q.get()
            logger.debug("Processing queue item %s for domain %s", message, domain)
            client = WireGuardClient(
                public_key=message,
                domain=domain,
                remove=False,
            )
            logger.info(
                f"Processing queue for key {client.public_key} on domain {domain} with lladdr {client.lladdr}"
            )
            logger.debug(link_handler(client))
            q.task_done()
        else:
            logger.debug("Queue is empty")
