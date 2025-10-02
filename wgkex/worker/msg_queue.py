#!/usr/bin/env python3
import json
import threading
from queue import Queue
from time import sleep

from wgkex.common import logger
from wgkex.worker.netlink import (
    ParkerWireGuardClient,
    WireGuardClient,
    link_handler,
    parker_link_handler,
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


def pick_from_queue(parker: bool = False) -> None:
    """Picks a message from the queue and processes it."""
    logger.debug("Starting queue processor")
    while True:
        if not q.empty():
            logger.debug("Queue is not empty current size is %i", q.qsize())

            if parker:
                message = json.loads(q.get())
                client = ParkerWireGuardClient(
                    # TODO use shared data class
                    public_key=message.get("PublicKey"),
                    range6=message.get("Range6"),
                    keepalive=message.get("Keepalive"),
                    remove=False,
                )
                logger.info(
                    f"Processing queue for key {client.public_key} with range {client.range6}"
                )
                logger.debug(parker_link_handler(client))
            else:
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
            sleep(1)
