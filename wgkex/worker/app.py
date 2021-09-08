"""Initialises the MQTT worker."""

import wgkex.config.config as config
from wgkex.worker import mqtt
from wgkex.worker.netlink import wg_flush_stale_peers
import threading
import time
import logging
import datetime
from typing import List, Text

logging.basicConfig(
    format="%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s",
    datefmt="%Y-%m-%d:%H:%M:%S",
    level=config.load_config().get("log_level"),
)

_CLEANUP_TIME = 300


class Error(Exception):
    """Base Exception handling class."""


class DomainsNotInConfig(Error):
    """If no domains exist in configuration file."""


def flush_workers(domain: Text) -> None:
    """Calls peer flush every _CLEANUP_TIME interval."""
    while True:
        time.sleep(_CLEANUP_TIME)
        logging.info(f"Running cleanup task for {domain}")
        logging.info("Cleaned up domains: %s", wg_flush_stale_peers(domain))


def clean_up_worker(domains: List[Text]) -> None:
    """Wraps flush_workers in a thread for all given domains.

    Arguments:
        domains: list of domains.
    """
    logging.debug("Cleaning up the following domains: %s", domains)
    prefix = config.load_config().get("domain_prefix")
    for domain in domains:
        logging.info("Scheduling cleanup task for %s, ", domain)
        try:
            cleaned_domain = domain.split(prefix)[1]
        except IndexError:
            logging.error(
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
    mqtt.connect(domains)


if __name__ == "__main__":
    main()
