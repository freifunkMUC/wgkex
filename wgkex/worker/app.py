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


class PrefixesNotInConfig(Error):
    """If no prefixes exist in configuration file."""


class DomainsAreNotUnique(Error):
    """If non-unique domains exist in configuration file."""


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
    prefixes = config.load_config().get("domain_prefix")
    cleanup_counter = 0
    # ToDo: do we need a check if every domain got gleaned?
    for prefix in prefixes:
        for domain in domains:
            if prefix in domain:
                logger.info("Scheduling cleanup task for %s, ", domain)
                try:
                    cleaned_domain = domain.split(prefix)[1]
                    cleanup_counter += 1
                except IndexError:
                    logger.error(
                        "Cannot strip domain with prefix %s from passed value %s. Skipping cleanup operation",
                        prefix,
                        domain,
                    )
                    continue
                thread = threading.Thread(target=flush_workers, args=(cleaned_domain,))
                thread.start()
    if cleanup_counter < len(domains):
        logger.error(
            "Not every domain got cleaned. Check domains for missing prefixes", repr(domains), repr(prefixes), )


def check_all_domains_unique(domains):
    """strips off prefixes and checks if domains are unique

    Args:
        domains: [str]
    Returns:
        boolean
    """
    prefixes = config.load_config().get("prefixes")
    if not prefixes:
        raise PrefixesNotInConfig("Could not locate prefixes in configuration.")
    unique_domains = []
    for domain in domains:
        for prefix in prefixes:
            if prefix in domain:
                stripped_domain = domain.split(prefix)[1]
                if stripped_domain in unique_domains:
                    logger.error(
                        "We have a non-unique domain here",
                        domain,
                    )
                    return False
                else:
                    unique_domains.append(stripped_domain)
    return True


def main():
    """Starts MQTT listener.

    Raises:
        DomainsNotInConfig: If no domains were found in configuration file.
        DomainsAreNotUnique: If there were non-unique domains after stripping prefix
    """
    domains = config.load_config().get("domains")
    if not domains:
        raise DomainsNotInConfig("Could not locate domains in configuration.")
    if not check_all_domains_unique(domains):
        raise DomainsAreNotUnique("There are non-unique domains! Check config.")
    clean_up_worker(domains)
    watch_queue()
    mqtt.connect()


if __name__ == "__main__":
    main()
