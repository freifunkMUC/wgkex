"""Configuration handling class."""

import dataclasses
import logging
import os
import sys
from enum import Enum
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
    """

    weight: int

    @classmethod
    def from_dict(cls, worker_cfg: Dict[str, Any]) -> "Worker":
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
class Parker:
    """A representation of the 'parker' key in Configuration file.

    Attributes:
        enabled: Whether Parker is enabled or not.
        464xlat: Whether 464xlat is used or not.
        prefixes: The prefixes configuration for Parker.
    """

    @dataclasses.dataclass
    class Prefixes:

        @dataclasses.dataclass
        class IPv4:
            clat_subnet: Optional[str] = None
            length: Optional[int] = None
            netbox_filter: Optional[Dict[str, Any]] = None
            netbox_additional_data: Optional[Dict[str, Any]] = None

        @dataclasses.dataclass
        class IPv6:
            length: int
            netbox_filter: Optional[Dict[str, Any]] = None
            netbox_additional_data: Optional[Dict[str, Any]] = None

        ipv4: IPv4
        ipv6: IPv6

    class IPAM(Enum):
        JSON = "json"
        NETBOX = "netbox"

    enabled: bool
    xlat: bool
    prefixes: Prefixes
    ipam: IPAM

    @classmethod
    def from_dict(cls, parker_cfg: Dict[str, Any]) -> "Parker":
        """Generates a parker config object from a dictionary.

        Args:
            parker_cfg: dictionary with parker config

        Returns:
            parker config object
        """

        enabled = bool(parker_cfg.get("enabled", False))
        xlat = bool(parker_cfg.get("464xlat", False))
        ipam = cls.IPAM(parker_cfg.get("ipam"))

        pfx = parker_cfg.get("prefixes", None)
        if pfx is None or not isinstance(pfx, dict):
            raise ValueError(
                "Parker is enabled, but no prefixes config is set in the config file"
            )
        if "ipv6" not in pfx or "ipv4" not in pfx:
            raise ValueError(
                "Parker prefixes config must contain both 'ipv4' and 'ipv6' keys"
            )

        if xlat:
            if "clat_subnet" not in pfx["ipv4"]:
                raise ValueError(
                    "Parker prefixes config must contain 'clat_subnet' key for 'ipv4' when using 464xlat"
                )
        else:
            if "length" not in pfx["ipv4"]:
                raise ValueError(
                    "Parker prefixes config must contain 'length' key for 'ipv4' when not using 464xlat"
                )
            if ipam == cls.IPAM.NETBOX and (
                "netbox_filter" not in pfx["ipv4"]
                or not isinstance(pfx["ipv4"]["netbox_filter"], dict)
            ):
                raise ValueError(
                    "Parker prefixes config must contain 'netbox_filter' key for 'ipv4' when using NetBox as IPAM and not using 464xlat"
                )

        if "length" not in pfx["ipv6"]:
            raise ValueError(
                "Parker prefixes config must contain 'length' key for 'ipv6' when not using 464xlat"
            )
        if ipam == cls.IPAM.NETBOX and (
            "netbox_filter" not in pfx["ipv6"]
            or not isinstance(pfx["ipv6"]["netbox_filter"], dict)
        ):
            raise ValueError(
                "Parker prefixes config must contain 'netbox_filter' key for 'ipv6' when using NetBox as IPAM and not using 464xlat"
            )

        # TODO remove this block when non-464XLAT mode is fully supported
        if not xlat:
            raise NotImplementedError("Non-464XLAT mode is not supported yet")

        return cls(
            enabled=enabled,
            xlat=xlat,
            prefixes=cls.Prefixes(
                ipv4=cls.Prefixes.IPv4(
                    clat_subnet=pfx["ipv4"].get("clat_subnet", None),
                    netbox_filter=pfx["ipv4"].get("netbox_filter", None),
                    netbox_additional_data=pfx["ipv4"].get(
                        "netbox_additional_data", None
                    ),
                    length=pfx["ipv4"].get("length", None),
                ),
                ipv6=cls.Prefixes.IPv6(
                    netbox_filter=pfx["ipv6"].get("netbox_filter", None),
                    netbox_additional_data=pfx["ipv6"].get(
                        "netbox_additional_data", None
                    ),
                    length=pfx["ipv6"]["length"],
                ),
            ),
            ipam=ipam,
        )


@dataclasses.dataclass
class Netbox:
    """A representation of the 'netbox' key in configuration file.

    Attributes:
        url: The NetBox URL to connect to.
        api_key: The API key to use for authentication.
    """

    url: str
    api_key: str

    @classmethod
    def from_dict(cls, netbox_cfg: Dict[str, str]) -> "Netbox":
        """Generates a netbox config object from a dictionary.

        Args:
            netbox_cfg: dictionary with netbox config

        Returns:
            netbox config object
        """
        return cls(
            url=netbox_cfg["url"],
            api_key=netbox_cfg["api_key"],
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
    workers: Workers
    parker: Parker
    external_name: Optional[str]
    mqtt: MQTT
    broker_listen: BrokerListen
    netbox: Optional[Netbox] = None
    broker_signing_key: Optional[str] = None

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
        parker = Parker.from_dict(cfg.get("parker", {}))
        broker_signing_key = cfg.get("broker_signing_key", None)

        if parker.enabled and broker_signing_key is None:
            raise ValueError(
                "Parker is enabled, but no broker_signing_key is set in the config file"
            )

        netbox_cfg = None
        if parker.enabled and parker.ipam == Parker.IPAM.NETBOX:
            if "netbox" not in cfg or not isinstance(cfg["netbox"], dict):
                raise ValueError(
                    "Parker is enabled with NetBox IPAM, but no netbox config is set in the config file"
                )
            netbox_cfg = Netbox.from_dict(cfg["netbox"])

        return cls(
            raw=cfg,
            domains=cfg["domains"],
            domain_prefixes=cfg["domain_prefixes"],
            broker_listen=broker_listen,
            mqtt=mqtt_cfg,
            workers=workers_cfg,
            external_name=cfg.get("externalName"),
            parker=parker,
            netbox=netbox_cfg,
            broker_signing_key=broker_signing_key,
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
