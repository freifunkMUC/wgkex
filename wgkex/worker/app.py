"""Initialises the MQTT worker."""

import wgkex.config.config as config
from wgkex.worker import mqtt


class Error(Exception):
    """Base Exception handling class."""


class DomainsNotInConfig(Error):
    """If no domains exist in configuration file."""


def main():
    """Starts MQTT listener.

    Raises:
        DomainsNotInConfig: If no domains were found in configuration file.
    """
    domains = config.load_config().get("domains")
    if not domains:
        raise DomainsNotInConfig("Could not locate domains in configuration.")
    mqtt.connect(domains)


if __name__ == "__main__":
    main()
