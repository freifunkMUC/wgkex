"""Initialises the MQTT worker."""

import signal
import sys
import threading
import time

from wgkex.common import logger
from wgkex.common.utils import is_valid_domain
from wgkex.config import config
from wgkex.worker import drain, mqtt, observability
from wgkex.worker.allowlist import is_allowlisted
from wgkex.worker.msg_queue import watch_queue
from wgkex.worker.netlink import wg_flush_stale_peers
from wgkex.worker.rebalance import peer_add_tracker


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


def flush_workers(domain: str) -> None:
    """Removes stale peers every peer_cleanup.interval seconds."""
    cleanup_cfg = config.get_config().peer_cleanup
    wg_interface = f"wg-{domain}"

    def keep(public_key: str) -> bool:
        if is_allowlisted(public_key):
            return True
        # Peers added by the key exchange may not have done their first handshake yet
        added_at = peer_add_tracker.added_at(wg_interface, public_key)
        return (
            added_at is not None
            and peer_add_tracker.now() - added_at < cleanup_cfg.stale_timeout
        )

    while True:
        try:
            time.sleep(cleanup_cfg.interval)
            logger.info(f"Running cleanup task for {domain}")
            removed = wg_flush_stale_peers(domain, cleanup_cfg.stale_timeout, keep=keep)
            logger.info("Removed %d stale peers from %s", len(removed), domain)
            logger.debug("Cleanup results: %s", removed)
        except Exception as e:  # noqa: BLE001
            # Don't crash the thread when an exception is encountered
            logger.error(
                "Exception during cleanup task for %s: %s", domain, e, exc_info=True
            )


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
            "Not every domain got cleaned. Check domains for missing prefixes: domains %r, prefixes %r",
            domains,
            prefixes,
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


def start_metrics_server() -> None:
    """Starts the optional Prometheus metrics listener."""
    metrics_cfg = config.get_config().metrics
    port = getattr(metrics_cfg, "worker_port", None)
    if isinstance(port, int) and port > 0:
        observability.start_http_server(port, metrics_cfg.worker_host)


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

    # SIGTERM is sent by e.g. docker stop and systemd
    signal.signal(signal.SIGINT, on_exit)
    signal.signal(signal.SIGTERM, on_exit)
    # Drain mode for planned maintenance, see README
    signal.signal(signal.SIGUSR1, lambda *_: drain.drain_state.request(True))
    signal.signal(signal.SIGUSR2, lambda *_: drain.drain_state.request(False))

    domains = config.get_config().domains
    prefixes = config.get_config().domain_prefixes
    if not domains:
        raise DomainsNotInConfig("Could not locate domains in configuration.")
    if not check_all_domains_unique(domains, prefixes):
        raise DomainsAreNotUnique("There are non-unique domains! Check config.")
    for domain in domains:
        if not is_valid_domain(domain):
            raise InvalidDomain(f"Domain {domain} has invalid prefix.")
    start_metrics_server()
    clean_up_worker()
    watch_queue()
    mqtt.connect(exit_event)


if __name__ == "__main__":
    main()
