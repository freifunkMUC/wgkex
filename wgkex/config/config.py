"""Configuration handling class."""

import dataclasses
import ipaddress
import logging
import os
import sys
from enum import Enum
from typing import Any, Dict, List, Optional, Set

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
        id: A unique, static ID for this worker. In Parker mode this is the
            concentrator ID sent to nodes, identifying the tunnel on the node, so it
            must never change for a given worker. If not set explicitly in the config,
            it defaults to the worker's position in the workers map.
        weight: The relative weight of a worker, defaults to 1.
        location: Optional location of the worker, preferred by clients requesting it.
        pop: The PoP (Point of Presence) this worker is located in (Parker multi-tunnel support).
    """

    weight: int
    location: str | None = None
    pop: str = ""
    id: int = 0

    @classmethod
    def from_dict(cls, worker_cfg: dict[str, Any]) -> "Worker":
        location = worker_cfg.get("location")
        worker_id = int(worker_cfg.get("id", 0))
        if not 0 <= worker_id <= 2**32 - 1:
            raise ValueError("Worker ID must be an unsigned 32-bit integer")
        return cls(
            weight=int(worker_cfg["weight"]) if worker_cfg.get("weight") else 1,
            location=str(location) if location else None,
            pop=str(worker_cfg.get("pop") or ""),
            id=worker_id,
        )


@dataclasses.dataclass
class Workers:
    """A representation of the 'workers' key in the configuration file.

    Attributes:
        total_weight: Calculated on init, the total weight of all configured workers.
        all_pops
        sticky_worker_tolerance: percentage between 0 and 1 as float
    """

    total_weight: int
    all_pops: set[str]
    sticky_worker_tolerance: float
    _workers: dict[str, Worker]

    @classmethod
    def from_dict(
        cls, workers_cfg: dict[str, dict[str, Any]], sticky_worker_tolerance: int = 10
    ) -> "Workers":
        d = {
            key: Worker.from_dict({"id": id, **value})
            for (id, (key, value)) in enumerate(workers_cfg.items(), start=1)
        }

        ids = [worker.id for worker in d.values()]
        if len(set(ids)) != len(ids):
            raise ValueError(
                "Worker IDs must be unique. Set explicit unique 'id' values in the workers config."
            )

        total = 0
        # add "empty" PoP to set to support dynamically added workers
        pops = {
            "",
        }
        for worker in d.values():
            total += worker.weight
            pops.add(worker.pop)
        total = max(total, 1)

        if not 0 <= sticky_worker_tolerance <= 100:
            raise ValueError(
                "sticky_worker_tolerance must be a percentage between 0 and 100"
            )

        return cls(
            total_weight=total,
            all_pops=pops,
            _workers=d,
            sticky_worker_tolerance=sticky_worker_tolerance / 100,
        )

    def get(self, worker: str) -> Worker | None:
        return self._workers.get(worker)

    def relative_worker_weight(self, worker_name: str) -> float:
        """Returns the share of all peers this worker should get according to the configured weights.

        Workers missing from the configuration get the average configured weight.
        """
        worker = self.get(worker_name)
        if worker is None:
            if worker_name not in _warned_unknown_workers:
                _warned_unknown_workers.add(worker_name)
                logging.warning(  # noqa: LOG015
                    "Worker %s is not in the workers configuration, using the average weight",
                    worker_name,
                )
            return self.average_weight() / self.total_weight
        return worker.weight / self.total_weight

    def location(self, worker_name: str) -> str | None:
        """Returns the configured location of a worker, if any."""
        worker = self.get(worker_name)
        return worker.location if worker is not None else None

    def average_weight(self) -> float:
        """Returns the average weight of the configured workers, 1 if none are configured."""
        if not self._workers:
            return 1
        return sum(w.weight for w in self._workers.values()) / len(self._workers)


# Workers not in the configuration we already warned about
_warned_unknown_workers: set[str] = set()


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
        drain_rate: [broker] Share of its peers a draining worker is asked to evict per interval.
    """

    enabled: bool = False
    interval: int = 60
    tolerance: float = 0.1
    min_excess_peers: int = 10
    stable_time: int = 600
    max_evictions_ratio: float = 0.01
    eviction_cooldown: int = 3600
    drain_rate: float = 0.1

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
            drain_rate=min(max(float(cfg.get("drain_rate", cls.drain_rate)), 0.0), 1.0),
        )


@dataclasses.dataclass
class Drain:
    """A representation of the 'drain' key in the configuration file (worker-only).

    A draining worker publishes status 2: new brokers stop assigning new peers to it and, with
    rebalancing enabled, ask it to shed its peers. Draining is started with SIGUSR1 (stopped with
    SIGUSR2) or while the configured file exists.

    Attributes:
        file: Optional path of a file, the worker is draining while it exists.
        check_interval: Seconds between checks of the drain file.
    """

    file: str | None = None
    check_interval: int = 5

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> "Drain":
        return cls(
            file=cfg.get("file") or None,
            check_interval=max(int(cfg.get("check_interval", cls.check_interval)), 1),
        )


@dataclasses.dataclass
class Metrics:
    """A representation of the 'metrics' key in the configuration file.

    Attributes:
        broker_endpoint: Serve Prometheus metrics at /metrics on the broker's HTTP listener.
        worker_port: Port of an HTTP listener serving Prometheus metrics on the worker,
            None (default) disables it.
        worker_host: Address the worker metrics listener binds to.
    """

    broker_endpoint: bool = True
    worker_port: int | None = None
    worker_host: str = "0.0.0.0"

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> "Metrics":
        port = cfg.get("worker_port")
        return cls(
            broker_endpoint=bool(cfg.get("broker_endpoint", cls.broker_endpoint)),
            worker_port=int(port) if port else None,
            worker_host=str(cfg.get("worker_host") or cls.worker_host),
        )


@dataclasses.dataclass
class LoadBalancing:
    """A representation of the 'load_balancing' key in the configuration file.

    Attributes:
        pending_assignment_ttl: [broker] Seconds a peer assigned by the v2 API counts towards the
            load of its worker if the worker metrics do not reflect it yet.
        pending_assignment_settle_time: [broker] Seconds after which a pending assignment is
            assumed to be included in newly received worker metrics.
        metrics_max_age: [broker] Seconds after which a worker is treated as offline for a domain
            if no connected peers metric was received for it. 0 disables the check.
        targeted_publish: [broker] Publish keys received via the v2 API only to the chosen worker
            (wireguard/{domain}/{worker}) instead of all workers, and ask the previously assigned
            worker to remove the peer when a key moves to another worker.
        previous_assignment_ttl: [broker] Seconds the broker remembers which worker a key was last
            assigned to, used for removing the peer from its previous worker.
        previous_assignment_max_entries: [broker] Maximum number of remembered assignments.
        location_tolerance: [broker] Workers in the location requested by a client are preferred
            while their effective peers exceed their weighted target by at most
            max(location_min_excess_peers, location_tolerance * target).
        location_min_excess_peers: [broker] See location_tolerance.
        share_pending_assignments: [broker] Publish assignments to other broker instances and
            count theirs as pending, for multiple brokers behind a load balancer.
        broker_id: [broker] Unique name of this broker instance, used for sharing assignments.
            Defaults to the hostname and a random suffix.
        rebalancing: Active rebalancing configuration.
    """

    pending_assignment_ttl: int = 300
    pending_assignment_settle_time: int = 60
    metrics_max_age: int = 180
    targeted_publish: bool = True
    previous_assignment_ttl: int = 21600
    previous_assignment_max_entries: int = 100000
    location_tolerance: float = 0.1
    location_min_excess_peers: int = 10
    share_pending_assignments: bool = False
    broker_id: str | None = None
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
            metrics_max_age=int(cfg.get("metrics_max_age", cls.metrics_max_age)),
            targeted_publish=bool(cfg.get("targeted_publish", cls.targeted_publish)),
            previous_assignment_ttl=int(
                cfg.get("previous_assignment_ttl", cls.previous_assignment_ttl)
            ),
            previous_assignment_max_entries=int(
                cfg.get(
                    "previous_assignment_max_entries",
                    cls.previous_assignment_max_entries,
                )
            ),
            location_tolerance=max(
                float(cfg.get("location_tolerance", cls.location_tolerance)), 0.0
            ),
            location_min_excess_peers=max(
                int(
                    cfg.get("location_min_excess_peers", cls.location_min_excess_peers)
                ),
                0,
            ),
            share_pending_assignments=bool(
                cfg.get("share_pending_assignments", cls.share_pending_assignments)
            ),
            broker_id=str(cfg["broker_id"]) if cfg.get("broker_id") else None,
            rebalancing=Rebalancing.from_dict(cfg.get("rebalancing") or {}),
        )


@dataclasses.dataclass
class RateLimit:
    """A representation of the 'rate_limit' key in the configuration file (broker-only).

    Rate-limited key exchange requests still get the normal response, but the key is not published
    again, so clients never fall back to the v1 API. Limits of 0 disable the respective check.

    Attributes:
        per_client_requests: Requests allowed per client address and per_client_period.
        per_client_period: Seconds in which per_client_requests are allowed (token bucket).
        global_requests: Requests allowed over all clients per global_period.
        global_period: Seconds in which global_requests are allowed.
        ipv6_prefix_length: IPv6 clients are limited per prefix of this length.
        max_tracked_clients: Maximum number of client addresses tracked at once.
    """

    per_client_requests: int = 30
    per_client_period: int = 60
    global_requests: int = 6000
    global_period: int = 60
    ipv6_prefix_length: int = 64
    max_tracked_clients: int = 100000

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> "RateLimit":
        def get(name: str, minimum: int) -> int:
            return max(int(cfg.get(name, getattr(cls, name))), minimum)

        return cls(
            per_client_requests=get("per_client_requests", 0),
            per_client_period=get("per_client_period", 1),
            global_requests=get("global_requests", 0),
            global_period=get("global_period", 1),
            ipv6_prefix_length=min(get("ipv6_prefix_length", 0), 128),
            max_tracked_clients=get("max_tracked_clients", 1),
        )


@dataclasses.dataclass
class Broker:
    """A representation of the 'broker' key in the configuration file (broker-only).

    Attributes:
        trusted_proxies: Reverse proxies whose X-Forwarded-For header is trusted to determine the
            client address (used for rate limiting and log_client_ip). Either a number of proxies
            every request passes through, or a list of proxy networks (CIDR) checked against the
            TCP peer address. 0 or empty: always use the TCP peer address.
        log_client_ip: Log the client address together with domain and key of each key exchange.
    """

    trusted_proxies: int | list[ipaddress.IPv4Network | ipaddress.IPv6Network] = 0
    log_client_ip: bool = False

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> "Broker":
        proxies = cfg.get("trusted_proxies") or 0
        if isinstance(proxies, str):
            proxies = [proxies]
        if isinstance(proxies, list):
            trusted: int | list = [
                ipaddress.ip_network(str(p).strip(), strict=False) for p in proxies
            ]
        else:
            trusted = max(int(proxies), 0)
        return cls(
            trusted_proxies=trusted,
            log_client_ip=bool(cfg.get("log_client_ip", False)),
        )


@dataclasses.dataclass
class KeyLists:
    """A representation of the 'key_lists' key in the configuration file.

    Files contain one public key per line ('#' starts a comment) and are reloaded when modified.

    Attributes:
        blocklist: [broker] Keys which are rejected by the key exchange and never published.
        blocklist_file: [broker] Optional file with additional blocked keys.
        allowlist: [worker] Keys which are never removed by the stale peer cleanup or evicted by
            rebalancing/draining.
        allowlist_file: [worker] Optional file with additional allowlisted keys.
        reload_interval: Seconds between checks of the files for modifications.
    """

    blocklist: list[str] = dataclasses.field(default_factory=list)
    blocklist_file: str | None = None
    allowlist: list[str] = dataclasses.field(default_factory=list)
    allowlist_file: str | None = None
    reload_interval: int = 60

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> "KeyLists":
        return cls(
            blocklist=[str(k) for k in cfg.get("blocklist") or []],
            blocklist_file=cfg.get("blocklist_file") or None,
            allowlist=[str(k) for k in cfg.get("allowlist") or []],
            allowlist_file=cfg.get("allowlist_file") or None,
            reload_interval=max(
                int(cfg.get("reload_interval", cls.reload_interval)), 1
            ),
        )


@dataclasses.dataclass
class PeerCleanup:
    """A representation of the 'peer_cleanup' key in the configuration file (worker-only).

    Attributes:
        stale_timeout: Seconds without a handshake after which a peer is removed.
        interval: Seconds between two cleanup runs.
    """

    stale_timeout: int = 1800
    interval: int = 300

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> "PeerCleanup":
        return cls(
            stale_timeout=int(cfg.get("stale_timeout", cls.stale_timeout)),
            interval=int(cfg.get("interval", cls.interval)),
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

        Arguments:
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
        xlat: Whether 464XLAT is used or not.
        prefixes: The prefixes configuration for Parker.
        ipam: The IPAM backend to use for the node prefixes (json or netbox).
        retry_interval: The retry interval in seconds for nodes to recontact the broker to check for new configuration.
        wg_keepalive: The keepalive interval in seconds the nodes should set for the WireGuard tunnel.
    """

    @dataclasses.dataclass
    class Prefixes:
        """A representation of the 'prefixes' key in the 'parker' configuration.

        Attributes:
            ipv4: The IPv4 prefix configuration.
            ipv6: The IPv6 prefix configuration.
        """

        @dataclasses.dataclass
        class IPv4:
            """A representation of the 'ipv4' key in the 'prefixes' configuration.

            Attributes:
                clat_subnet: The CLAT subnet to use for 464XLAT, i.e. the client subnet behind the nodes.
                length: The subnet size to use for the IPv4 subnet when not using 464XLAT.
                netbox_filter: The filter to use when querying NetBox for an IPv4 prefix when not using 464XLAT.
                netbox_additional_data: Additional data to send to NetBox when reserving an IPv4 prefix when not using 464XLAT.
            """

            clat_subnet: Optional[str] = None
            length: Optional[int] = None
            netbox_filter: Optional[Dict[str, Any]] = None
            netbox_additional_data: Optional[Dict[str, Any]] = None

        @dataclasses.dataclass
        class IPv6:
            """A representation of the 'ipv6' key in the 'prefixes' configuration.

            Attributes:
                length: The prefix size to use for the IPv6 prefix.
                netbox_filter: The filter to use when querying NetBox for an IPv6 prefix.
                netbox_additional_data: Additional data to send to NetBox when reserving an IPv6 prefix.
            """

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
    retry_interval: int = 120
    wg_keepalive: int = 25

    @classmethod
    def from_dict(cls, parker_cfg: Dict[str, Any]) -> "Parker":
        """Generates a parker config object from a dictionary.

        Arguments:
            parker_cfg: dictionary with parker config

        Returns:
            parker config object
        """

        enabled = bool(parker_cfg.get("enabled", False))

        if not enabled:
            return cls(
                enabled=enabled,
                xlat=False,
                prefixes=cls.Prefixes(
                    ipv4=cls.Prefixes.IPv4(),
                    ipv6=cls.Prefixes.IPv6(length=0, netbox_filter={}),
                ),
                ipam=cls.IPAM.JSON,
            )

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
        ipv6_prefix_length = int(pfx["ipv6"]["length"])
        if not 0 <= ipv6_prefix_length <= 63:
            raise ValueError(
                "Parker IPv6 allocation prefix length must be between 0 and 63"
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

        retry_interval = int(parker_cfg.get("retry_interval", cls.retry_interval))
        wg_keepalive = int(parker_cfg.get("wg_keepalive", cls.wg_keepalive))

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
                    length=ipv6_prefix_length,
                ),
            ),
            ipam=ipam,
            retry_interval=retry_interval,
            wg_keepalive=wg_keepalive,
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

        Arguments:
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
        external_name: The publicly resolvable domain name or public IP address of this worker (worker-only).
        load_balancing: Load balancing and rebalancing configuration.
        peer_cleanup: Stale peer cleanup configuration (worker-only).
        drain: Drain mode configuration (worker-only).
        metrics: Prometheus metrics configuration.
        rate_limit: Key exchange rate limiting (broker-only).
        key_lists: Key block- and allowlists.
        broker: Client address handling of the broker.
    """

    raw: dict[str, Any]
    domains: list[str]
    domain_prefixes: list[str]
    broker_listen: BrokerListen
    mqtt: MQTT
    workers: Workers
    external_name: str | None
    parker: Parker
    load_balancing: LoadBalancing = dataclasses.field(default_factory=LoadBalancing)
    peer_cleanup: PeerCleanup = dataclasses.field(default_factory=PeerCleanup)
    drain: Drain = dataclasses.field(default_factory=Drain)
    metrics: Metrics = dataclasses.field(default_factory=Metrics)
    rate_limit: RateLimit = dataclasses.field(default_factory=RateLimit)
    key_lists: KeyLists = dataclasses.field(default_factory=KeyLists)
    broker: Broker = dataclasses.field(default_factory=Broker)
    netbox: Netbox | None = None
    broker_signing_key: str | None = None

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
        workers_cfg = Workers.from_dict(
            cfg.get("workers", {}), cfg.get("sticky_worker_tolerance", 10)
        )
        parker = Parker.from_dict(cfg.get("parker", {}))
        broker_signing_key = cfg.get("broker_signing_key", None)

        netbox_cfg = None
        if isinstance(cfg.get("netbox"), dict) and {
            "url",
            "api_key",
        }.issubset(cfg["netbox"]):
            netbox_cfg = Netbox.from_dict(cfg["netbox"])

        return cls(
            raw=cfg,
            domains=cfg["domains"],
            domain_prefixes=cfg["domain_prefixes"],
            broker_listen=broker_listen,
            mqtt=mqtt_cfg,
            workers=workers_cfg,
            external_name=cfg.get("externalName"),
            load_balancing=LoadBalancing.from_dict(cfg.get("load_balancing") or {}),
            peer_cleanup=PeerCleanup.from_dict(cfg.get("peer_cleanup") or {}),
            drain=Drain.from_dict(cfg.get("drain") or {}),
            metrics=Metrics.from_dict(cfg.get("metrics") or {}),
            rate_limit=RateLimit.from_dict(cfg.get("rate_limit") or {}),
            key_lists=KeyLists.from_dict(cfg.get("key_lists") or {}),
            broker=Broker.from_dict(cfg.get("broker") or {}),
            parker=parker,
            netbox=netbox_cfg,
            broker_signing_key=broker_signing_key,
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
