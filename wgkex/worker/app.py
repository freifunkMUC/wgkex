"""Initialises the MQTT worker."""

import signal
import sys
import threading
import time
from typing import Text

from wgkex.common import logger
from wgkex.common.utils import is_valid_domain
from wgkex.config import config
from wgkex.worker import mqtt
from wgkex.worker.msg_queue import watch_queue
from wgkex.worker.netlink import wg_flush_stale_peers

_CLEANUP_TIME = 3600


class Error(Exception):
    """Base Exception handling class."""


class DomainsNotInConfig(Error):
    """If no domains exist in configuration file."""


class PrefixesNotInConfig(Error):
    """If no prefixes exist in configuration file."""


class DomainsAreNotUnique(Error):
    """If non-unique domains exist in configuration file."""


class InvalidDomain(Error):
    """If the domains is invalid and is not listed in the configuration file."""


def flush_workers(domain: Text) -> None:
    """Calls peer flush every _CLEANUP_TIME interval."""
    while True:
        try:
            time.sleep(_CLEANUP_TIME)
            logger.info(f"Running cleanup task for {domain}")
            logger.info("Cleaned up domains: %s", wg_flush_stale_peers(domain))
        except Exception as e:
            # Don't crash the thread when an exception is encountered
            logger.error(f"Exception during cleanup task for {domain}:")
            logger.error(e)


def clean_up_worker() -> None:
    """Wraps flush_workers in a thread for all given domains.

    Arguments:
        domains: list of domains.
    """
    domains = config.get_config().domains
    prefixes = config.get_config().domain_prefixes
    logger.debug("Cleaning up the following domains: %s", domains)
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
                thread = threading.Thread(
                    target=flush_workers, args=(cleaned_domain,), daemon=True
                )
                thread.start()
    if cleanup_counter < len(domains):
        logger.error(
            "Not every domain got cleaned. Check domains for missing prefixes",
            repr(domains),
            repr(prefixes),
        )


def check_all_domains_unique(domains, prefixes):
    """strips off prefixes and checks if domains are unique

    Args:
        domains: [str]
    Returns:
        boolean
    """
    if not prefixes:
        raise PrefixesNotInConfig("Could not locate prefixes in configuration.")
    if not isinstance(prefixes, list):
        raise TypeError("prefixes is not a list")
    unique_domains = []
    for domain in domains:
        for prefix in prefixes:
            if prefix in domain:
                stripped_domain = domain.split(prefix)[1]
                if stripped_domain in unique_domains:
                    logger.error(
                        f"Domain {domain} is not unique after stripping the prefix"
                    )
                    return False
                unique_domains.append(stripped_domain)
    return True


def main():
    """Starts MQTT listener.

    Raises:
        DomainsNotInConfig: If no domains were found in configuration file.
        DomainsAreNotUnique: If there were non-unique domains after stripping prefix
    """
    exit_event = threading.Event()

    def on_exit(sig_number, stack_frame) -> None:
        logger.info("Shutting down...")
        exit_event.set()
        time.sleep(2)
        sys.exit()

    signal.signal(signal.SIGINT, on_exit)

    parker_enabled = config.get_config().parker.enabled
    if parker_enabled:
        logger.info("Parker mode is enabled")
    else:

        domains = config.get_config().domains
        prefixes = config.get_config().domain_prefixes
        if not domains:
            raise DomainsNotInConfig("Could not locate domains in configuration.")
        if not check_all_domains_unique(domains, prefixes):
            raise DomainsAreNotUnique("There are non-unique domains! Check config.")
        for domain in domains:
            if not is_valid_domain(domain):
                raise InvalidDomain(f"Domain {domain} has invalid prefix.")
        clean_up_worker()

    watch_queue(parker_enabled)
    mqtt.connect(exit_event)


if __name__ == "__main__":
    main()
