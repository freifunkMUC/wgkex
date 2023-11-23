"""Initialises the MQTT worker."""

import wgkex.config.config as config
from wgkex.worker import mqtt
from wgkex.worker.msg_queue import watch_queue
from wgkex.worker.netlink import wg_flush_stale_peers
import time
import threading
from wgkex.common import logger
from typing import List, Text

_CLEANUP_TIME = 3600


class Error(Exception):
    """Base Exception handling class."""


class DomainsNotInConfig(Error):
    """If no domains exist in configuration file."""


def flush_workers(domain: Text) -> None:
    """Calls peer flush every _CLEANUP_TIME interval."""
    while True:
        time.sleep(_CLEANUP_TIME)
        logger.info(f"Running cleanup task for {domain}")
        logger.info("Cleaned up domains: %s", wg_flush_stale_peers(domain))


def clean_up_worker(domains: List[Text]) -> None:
    """Wraps flush_workers in a thread for all given domains.

    Arguments:
        domains: list of domains.
    """
    logger.debug("Cleaning up the following domains: %s", domains)
    prefix = config.load_config().get("domain_prefix")  # ToDo
    for domain in domains:
        logger.info("Scheduling cleanup task for %s, ", domain)
        try:
            cleaned_domain = domain.split(prefix)[1]
        except IndexError:
            logger.error(
                "Cannot strip domain with prefix %s from passed value %s. Skipping cleanup operation",
                prefix,
                domain,
            )
            continue
        thread = threading.Thread(target=flush_workers, args=(cleaned_domain,))
        thread.start()


def main():
    """Starts MQTT listener.

    Raises:
        DomainsNotInConfig: If no domains were found in configuration file.
    """
    domains = config.load_config().get("domains")
    if not domains:
        raise DomainsNotInConfig("Could not locate domains in configuration.")
    clean_up_worker(domains)
    watch_queue()
    mqtt.connect()


if __name__ == "__main__":
    main()
