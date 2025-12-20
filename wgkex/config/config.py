"""Configuration handling class."""

import dataclasses
import logging
import os
import sys
from typing import Any, Dict, List, Optional

import yaml


class Error(Exception):
    """Base Exception handling class."""


class ConfigFileNotFoundError(Error):
    """File could not be found on disk."""


WG_CONFIG_OS_ENV = "WGKEX_CONFIG_FILE"
WG_CONFIG_DEFAULT_LOCATION = "/etc/wgkex.yaml"


@dataclasses.dataclass
class Worker:
    """A representation of the values of the 'workers' dict in the configuration file.

    Attributes:
        weight: The relative weight of a worker, defaults to 1.
        location: Optional location identifier for this worker (e.g., "MUC", "Vienna").
    """

    weight: int
    location: Optional[str] = None

    @classmethod
    def from_dict(cls, worker_cfg: Dict[str, Any]) -> "Worker":
        return cls(
            weight=int(worker_cfg["weight"]) if worker_cfg["weight"] else 1,
            location=worker_cfg.get("location"),
        )


@dataclasses.dataclass
class Workers:
    """A representation of the 'workers' key in the configuration file.

    Attributes:
        total_weight: Calculated on init, the total weight of all configured workers.
    """

    total_weight: int
    _workers: Dict[str, Worker]

    @classmethod
    def from_dict(cls, workers_cfg: Dict[str, Dict[str, Any]]) -> "Workers":
        d = {key: Worker.from_dict(value) for (key, value) in workers_cfg.items()}

        total = 0
        for worker in d.values():
            total += worker.weight
        total = max(total, 1)

        return cls(total_weight=total, _workers=d)

    def get(self, worker: str) -> Optional[Worker]:
        return self._workers.get(worker)

    def relative_worker_weight(self, worker_name: str) -> float:
        worker = self.get(worker_name)
        if worker is None:
            return 1 / self.total_weight
        return worker.weight / self.total_weight

    def get_locations(self) -> List[str]:
        """Returns a list of unique locations configured for workers.
        
        Returns:
            A list of location strings, excluding None values.
        """
        locations = set()
        for worker in self._workers.values():
            if worker.location:
                locations.add(worker.location)
        return sorted(list(locations))

    def get_workers_by_location(self, location: str) -> List[str]:
        """Returns a list of worker names that match the given location.
        
        Args:
            location: The location to filter by.
            
        Returns:
            A list of worker names matching the location.
        """
        return [
            name for name, worker in self._workers.items()
            if worker.location == location
        ]


@dataclasses.dataclass
class BrokerListen:
    """A representation of the 'broker_listen' key in Configuration file.

    Attributes:
        host: The listen address the broker should listen to for the HTTP API.
        port: The port the broker should listen to for the HTTP API.
    """

    host: Optional[str]
    port: Optional[int]

    @classmethod
    def from_dict(cls, broker_listen: Dict[str, Any]) -> "BrokerListen":
        return cls(
            host=broker_listen.get("host"),
            port=broker_listen.get("port"),
        )


@dataclasses.dataclass
class MQTT:
    """A representation of the 'mqtt' key in Configuration file.

    Attributes:
        broker_url: The broker URL for MQTT to connect to.
        username: The username to use for MQTT.
        password: The password to use for MQTT.
        tls: If TLS is used or not.
        broker_port: The port for MQTT to connect on.
        keepalive: The keepalive in seconds to use.
    """

    broker_url: str
    username: str
    password: str
    tls: bool = False
    broker_port: int = 1883
    keepalive: int = 5

    @classmethod
    def from_dict(cls, mqtt_cfg: Dict[str, str]) -> "MQTT":
        """seems to generate a mqtt config object from dictionary

        Args:
            mqtt_cfg ():

        Returns:
            mqtt config object
        """
        return cls(
            broker_url=mqtt_cfg["broker_url"],
            username=mqtt_cfg["username"],
            password=mqtt_cfg["password"],
            tls=bool(mqtt_cfg.get("tls", cls.tls)),
            broker_port=int(mqtt_cfg.get("broker_port", cls.broker_port)),
            keepalive=int(mqtt_cfg.get("keepalive", cls.keepalive)),
        )


@dataclasses.dataclass
class Config:
    """A representation of the configuration file.

    Attributes:
        domains: The list of domains to listen for.
        domain_prefixes: The list of prefixes to pre-pend to a given domain.
        mqtt: The MQTT configuration.
        workers: The worker weights configuration (broker-only).
        externalName: The publicly resolvable domain name or public IP address of this worker (worker-only).
    """

    raw: Dict[str, Any]
    domains: List[str]
    domain_prefixes: List[str]
    broker_listen: BrokerListen
    mqtt: MQTT
    workers: Workers
    external_name: Optional[str]

    @classmethod
    def from_dict(cls, cfg: Dict[str, Any]) -> "Config":
        """Creates a Config object from a configuration file.
        Arguments:
            cfg: The configuration file as a dict.
        Returns:
            A Config object.
        """
        broker_listen = BrokerListen.from_dict(cfg.get("broker_listen", {}))
        mqtt_cfg = MQTT.from_dict(cfg["mqtt"])
        workers_cfg = Workers.from_dict(cfg.get("workers", {}))
        return cls(
            raw=cfg,
            domains=cfg["domains"],
            domain_prefixes=cfg["domain_prefixes"],
            broker_listen=broker_listen,
            mqtt=mqtt_cfg,
            workers=workers_cfg,
            external_name=cfg.get("externalName"),
        )

    def get(self, key: str) -> Any:
        """Get the value of key from the raw dict representation of the config file"""
        return self.raw.get(key)


_parsed_config: Optional[Config] = None


def get_config() -> Config:
    """Returns a parsed Config object.

    Raises:
        ConfigFileNotFoundError: If we could not find the configuration file on disk.
    Returns:
        The Config representation of the config file
    """
    global _parsed_config
    if _parsed_config is None:
        cfg_contents = fetch_config_from_disk()
        try:
            config = yaml.safe_load(cfg_contents)
        except yaml.YAMLError as e:
            print("Failed to load YAML file: %s" % e)
            sys.exit(1)
        try:
            config = Config.from_dict(config)
        except (KeyError, TypeError, AttributeError) as e:
            print("Failed to lint file: %s" % e)
            sys.exit(2)
        _parsed_config = config
    return _parsed_config


def fetch_config_from_disk() -> str:
    """Fetches config file from disk and returns as string.

    Raises:
        ConfigFileNotFoundError: If we could not find the configuration file on disk.
    Returns:
        The file contents as string.
    """
    config_file = os.environ.get(WG_CONFIG_OS_ENV, WG_CONFIG_DEFAULT_LOCATION)
    logging.debug("getting config_file: %s", repr(config_file))
    try:
        with open(config_file, "r") as stream:
            return stream.read()
    except FileNotFoundError as e:
        raise ConfigFileNotFoundError(
            f"Could not locate configuration file in {config_file}"
        ) from e
