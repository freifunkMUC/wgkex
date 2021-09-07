"""Initialises the MQTT worker."""

import wgkex.config.config as config
from wgkex.worker import mqtt
from wgkex.worker.netlink import wg_flush_stale_peers
import threading
import time


class Error(Exception):
    """Base Exception handling class."""


class DomainsNotInConfig(Error):
    """If no domains exist in configuration file."""


def clean_up_worker(domain: str) -> None:
    while True:
        time.sleep(300)
        wg_flush_stale_peers(domain)


def main():
    """Starts MQTT listener.

    Raises:
        DomainsNotInConfig: If no domains were found in configuration file.
    """
    domains = config.load_config().get("domains")
    if not domains:
        raise DomainsNotInConfig("Could not locate domains in configuration.")
    clean_up_threads = []
    for domain in domains:
        thread = threading.Thread(
            target=clean_up_worker, args=(domain.split("ffmuc_")[1],)
        )
        thread.start()
        clean_up_threads.append(thread)
    mqtt.connect(domains)


if __name__ == "__main__":
    main()
