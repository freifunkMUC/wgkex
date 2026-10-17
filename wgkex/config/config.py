"""Configuration handling class."""

import dataclasses
import logging
import os
import sys
from typing import Any

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
    """

    weight: int

    @classmethod
    def from_dict(cls, worker_cfg: dict[str, Any]) -> "Worker":
        return cls(
            weight=int(worker_cfg["weight"]) if worker_cfg["weight"] else 1,
        )


@dataclasses.dataclass
class Workers:
    """A representation of the 'workers' key in the configuration file.

    Attributes:
        total_weight: Calculated on init, the total weight of all configured workers.
    """

    total_weight: int
    _workers: dict[str, Worker]

    @classmethod
    def from_dict(cls, workers_cfg: dict[str, dict[str, Any]]) -> "Workers":
        d = {key: Worker.from_dict(value) for (key, value) in workers_cfg.items()}

        total = 0
        for worker in d.values():
            total += worker.weight
        total = max(total, 1)

        return cls(total_weight=total, _workers=d)

    def get(self, worker: str) -> Worker | None:
        return self._workers.get(worker)

    def relative_worker_weight(self, worker_name: str) -> float:
        worker = self.get(worker_name)
        if worker is None:
            return 1 / self.total_weight
        return worker.weight / self.total_weight


@dataclasses.dataclass
class Rebalancing:
    """A representation of the 'load_balancing.rebalancing' key in the configuration file.

    Active rebalancing makes overloaded workers drop a small number of connected peers per interval.
    Gluon nodes notice the lost tunnel and redo the key exchange, which lets the broker assign them
    to an underloaded worker. Both broker and worker need to have it enabled.

    Attributes:
        enabled: [broker, worker] Whether active rebalancing is enabled. Defaults to False.
        interval: [broker, worker] Seconds between rebalancing rounds.
        tolerance: [broker] Relative deviation from a worker's weighted target that is tolerated.
        min_excess_peers: [broker] Absolute number of excess peers that is always tolerated.
        stable_time: [broker] Seconds without any worker going online/offline before rebalancing.
        max_evictions_ratio: [worker] Max share of connected peers evicted per interval (at least 1).
        eviction_cooldown: [worker] Seconds during which a peer is not evicted again.
    """

    enabled: bool = False
    interval: int = 60
    tolerance: float = 0.1
    min_excess_peers: int = 10
    stable_time: int = 600
    max_evictions_ratio: float = 0.01
    eviction_cooldown: int = 3600

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> "Rebalancing":
        return cls(
            enabled=bool(cfg.get("enabled", cls.enabled)),
            interval=int(cfg.get("interval", cls.interval)),
            tolerance=float(cfg.get("tolerance", cls.tolerance)),
            min_excess_peers=int(cfg.get("min_excess_peers", cls.min_excess_peers)),
            stable_time=int(cfg.get("stable_time", cls.stable_time)),
            max_evictions_ratio=float(
                cfg.get("max_evictions_ratio", cls.max_evictions_ratio)
            ),
            eviction_cooldown=int(cfg.get("eviction_cooldown", cls.eviction_cooldown)),
        )


@dataclasses.dataclass
class LoadBalancing:
    """A representation of the 'load_balancing' key in the configuration file.

    Attributes:
        pending_assignment_ttl: [broker] Seconds a peer assigned by the v2 API counts towards the
            load of its worker if the worker metrics do not reflect it yet.
        pending_assignment_settle_time: [broker] Seconds after which a pending assignment is
            assumed to be included in newly received worker metrics.
        rebalancing: Active rebalancing configuration.
    """

    pending_assignment_ttl: int = 300
    pending_assignment_settle_time: int = 60
    rebalancing: Rebalancing = dataclasses.field(default_factory=Rebalancing)

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> "LoadBalancing":
        return cls(
            pending_assignment_ttl=int(
                cfg.get("pending_assignment_ttl", cls.pending_assignment_ttl)
            ),
            pending_assignment_settle_time=int(
                cfg.get(
                    "pending_assignment_settle_time", cls.pending_assignment_settle_time
                )
            ),
            rebalancing=Rebalancing.from_dict(cfg.get("rebalancing") or {}),
        )


@dataclasses.dataclass
class BrokerListen:
    """A representation of the 'broker_listen' key in Configuration file.

    Attributes:
        host: The listen address the broker should listen to for the HTTP API.
        port: The port the broker should listen to for the HTTP API.
    """

    host: str | None
    port: int | None

    @classmethod
    def from_dict(cls, broker_listen: dict[str, Any]) -> "BrokerListen":
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
    def from_dict(cls, mqtt_cfg: dict[str, str]) -> "MQTT":
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
        load_balancing: Load balancing and rebalancing configuration.
    """

    raw: dict[str, Any]
    domains: list[str]
    domain_prefixes: list[str]
    broker_listen: BrokerListen
    mqtt: MQTT
    workers: Workers
    external_name: str | None
    load_balancing: LoadBalancing = dataclasses.field(default_factory=LoadBalancing)

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> "Config":
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
            load_balancing=LoadBalancing.from_dict(cfg.get("load_balancing") or {}),
        )

    def get(self, key: str) -> Any:
        """Get the value of key from the raw dict representation of the config file"""
        return self.raw.get(key)


_parsed_config: Config | None = None


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
            print(f"Failed to load YAML file: {e}")
            sys.exit(1)
        try:
            config = Config.from_dict(config)
        except (KeyError, TypeError, AttributeError) as e:
            print(f"Failed to lint file: {e}")
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
    logging.debug("getting config_file: %s", repr(config_file))  # noqa: LOG015
    try:
        with open(config_file, "r") as stream:
            return stream.read()
    except FileNotFoundError as e:
        raise ConfigFileNotFoundError(
            f"Could not locate configuration file in {config_file}"
        ) from e
