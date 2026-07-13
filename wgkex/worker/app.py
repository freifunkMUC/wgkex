"""Initialises the MQTT worker."""

import signal
import sys
import threading
from typing import Text

from wgkex.common import logger
from wgkex.common.utils import is_valid_domain
from wgkex.config import config
from wgkex.worker import mqtt
from wgkex.worker.msg_queue import watch_queue
from wgkex.worker.netlink import wg_flush_stale_peers
from wgkex.worker.peer_state import PeerMutationCoordinator, peer_mutations


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


def flush_workers(
    exit_event: threading.Event,
    parker: bool,
    domain: Text,
    cleanup: config.Cleanup,
    parker_prefix_length: int = 63,
    coordinator: PeerMutationCoordinator = peer_mutations,
) -> None:
    """Flush stale peers on an interruptible interval until shutdown."""
    while not exit_event.wait(cleanup.interval):
        try:
            logger.info("Running cleanup task domain=%s parker=%s", domain, parker)
            results = wg_flush_stale_peers(
                parker,
                domain,
                stale_timeout=cleanup.stale_timeout(parker),
                initial_handshake_grace=cleanup.initial_handshake_grace,
                parker_prefix_length=parker_prefix_length,
                coordinator=coordinator,
            )
            logger.info(
                "Cleanup task completed domain=%s parker=%s selected=%s "
                "deleted=%s deferred=%s failed=%s",
                domain,
                parker,
                len(results),
                sum(result.status == "deleted" for result in results),
                sum(result.status == "deferred" for result in results),
                sum(result.status == "failed" for result in results),
            )
        except Exception as error:
            logger.error(
                "Cleanup sweep failed domain=%s parker=%s",
                domain,
                parker,
                exc_info=error,
            )


def clean_up_worker(
    parker: bool,
    exit_event: threading.Event,
    worker_config: config.Config,
    coordinator: PeerMutationCoordinator = peer_mutations,
) -> list[threading.Thread]:
    """Wraps flush_workers in a thread for all given domains.

    Arguments:
        parker: Whether Parker mode is enabled.
    """
    if parker:
        thread = threading.Thread(
            target=flush_workers,
            args=(
                exit_event,
                parker,
                "parker",
                worker_config.cleanup,
                worker_config.parker.prefixes.ipv6.length,
                coordinator,
            ),
            name="peer-cleanup-parker",
        )
        thread.start()
        return [thread]

    domains = worker_config.domains
    prefixes = worker_config.domain_prefixes
    logger.debug("Cleaning up the following domains: %s", domains)
    cleanup_counter = 0
    threads = []
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
                    target=flush_workers,
                    args=(
                        exit_event,
                        parker,
                        cleaned_domain,
                        worker_config.cleanup,
                        63,
                        coordinator,
                    ),
                    name=f"peer-cleanup-{cleaned_domain}",
                )
                thread.start()
                threads.append(thread)
    if cleanup_counter < len(domains):
        logger.error(
            "Not every domain got cleaned. Check domains %s for missing prefixes %s",
            repr(domains),
            repr(prefixes),
        )
    return threads


def check_all_domains_unique(domains, prefixes):
    """strips off prefixes and checks if domains are unique

    Arguments:
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
        sys.exit()

    signal.signal(signal.SIGINT, on_exit)
    signal.signal(signal.SIGTERM, on_exit)

    worker_config = config.get_config()
    parker_enabled = worker_config.parker.enabled
    if parker_enabled:
        logger.info("Parker mode is enabled")
    else:
        domains = worker_config.domains
        prefixes = worker_config.domain_prefixes
        if not domains:
            raise DomainsNotInConfig("Could not locate domains in configuration.")
        if not check_all_domains_unique(domains, prefixes):
            raise DomainsAreNotUnique("There are non-unique domains! Check config.")
        for domain in domains:
            if not is_valid_domain(domain):
                raise InvalidDomain(f"Domain {domain} has invalid prefix.")

    cleanup_threads = clean_up_worker(parker_enabled, exit_event, worker_config)
    queue_thread = watch_queue(
        parker_enabled,
        exit_event,
        parker_prefix_length=worker_config.parker.prefixes.ipv6.length,
        initial_handshake_grace=worker_config.cleanup.initial_handshake_grace,
    )
    try:
        mqtt.connect(exit_event)
    finally:
        exit_event.set()
        for thread in [queue_thread, *cleanup_threads]:
            thread.join()


if __name__ == "__main__":
    main()
