#!/usr/bin/env python3
import queue
import threading
from wgkex.common import logger
from wgkex.worker.netlink import link_handler
from wgkex.worker.netlink import WireGuardClient

q = queue.Queue()

def watch_queue() -> None:
    """Watches the queue for new messages."""
    threading.Thread(target=worker, daemon=True).start()
    while q.empty() != True:
        pick_from_queue()

def pick_from_queue() -> None:
    """Picks a message from the queue and processes it."""
    domain, message = q.get()
    logger.debug("Processing queue item %s for domain %s", message, domain)
    client = WireGuardClient(
        public_key=str(message.payload.decode("utf-8")),
        domain=domain,
        remove=False,
    )
    logger.debug(link_handler(client))
    q.task_done()