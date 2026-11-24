"""Configuration handling class."""

import copy
import dataclasses
import ipaddress
import logging
import math
import os
import re
import sys
from enum import Enum
from typing import Any

import yaml


class Error(Exception):
    """Base Exception handling class."""


class ConfigFileNotFoundError(Error):
    """File could not be found on disk."""


def _positive_duration(value: Any, name: str) -> float:
    """Parse a finite, positive duration in seconds."""
    if isinstance(value, bool):
        message = f"{name} must be a positive duration in seconds"
        raise ValueError(message)  # noqa: TRY004
    try:
        duration = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a positive duration in seconds") from error
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"{name} must be a positive duration in seconds")
    return duration


# Same check as the node (parker/dhcp.lua): dnsmasq splits option values at
# commas, and a DHCP option holds at most 255 bytes.
_CAPTIVE_PORTAL_URL = re.compile(r"https://[A-Za-z0-9\-._~:/?@!$&*+;=%\[\]]+")


def _captive_portal_urls(value: Any) -> dict[str, str]:
    """Parse parker.captive_portal_urls: Gluon domain -> captive portal API URI."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("parker.captive_portal_urls must be a mapping")  # noqa: TRY004
    urls = {}
    for domain, url in value.items():
        if (
            not isinstance(url, str)
            or len(url.encode()) > 255
            or not _CAPTIVE_PORTAL_URL.fullmatch(url)
        ):
            raise ValueError(
                f"parker.captive_portal_urls.{domain} must be an https URI of at most "
                "255 bytes without comma, space, quote or '#'"
            )
        urls[str(domain)] = url
    return urls


def _route_metric(value: Any) -> int:
    """Parse a kernel route metric (1 to 2**32 - 1)."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2**32:
        raise ValueError("parker.route_gate.route_metric must be an integer >= 1")
    return value


WG_CONFIG_OS_ENV = "WGKEX_CONFIG_FILE"
WG_CONFIG_DEFAULT_LOCATION = "/etc/wgkex.yaml"
WG_CONFIG_ENV_PREFIX = "WGKEX__"
# Keys that are not lower case in the configuration file.
_ENV_KEY_CASE = {"externalname": "externalName"}
# Credentials are taken verbatim, so a password like "0123" or "true" is not converted.
_ENV_RAW_KEYS = {"username", "password"}


def _parse_env_value(key: str, value: str) -> Any:
    """Reads an override like a value in the YAML file; usernames and passwords stay strings."""
    if key.lower() in _ENV_RAW_KEYS:
        return value
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError:
        # Not the parser message: it could quote part of a secret.
        raise ValueError("invalid YAML value") from None


def _env_key(node: dict[str, Any], part: str) -> str:
    lower = part.lower()
    for key in node:
        if isinstance(key, str) and key.lower() == lower:
            return key
    return _ENV_KEY_CASE.get(lower, lower)


def env_overrides_present(environ: dict[str, str] | None = None) -> bool:
    """Returns True if any WGKEX__ override is set in the environment."""
    environ = os.environ if environ is None else environ
    return any(name.startswith(WG_CONFIG_ENV_PREFIX) for name in environ)


def apply_env_overrides(
    cfg: dict[str, Any], environ: dict[str, str] | None = None
) -> dict[str, Any]:
    """Applies WGKEX__<KEY>[__<SUBKEY>...] environment variables on top of the configuration.

    WGKEX__MQTT__PASSWORD=secret sets mqtt.password. Keys match the configuration file case-insensitively,
    values are read like in the YAML file (except usernames and passwords, which stay strings).
    Variables are applied in sorted order, so a nested variable wins over a map set by its parent variable.

    Arguments:
        cfg: The configuration file as a dict (not modified).
        environ: The environment, defaults to os.environ.

    Raises:
        ValueError: If a variable name is malformed, a value cannot be parsed or a key is not a map.
    Returns:
        A new dict with the overrides applied.
    """
    environ = os.environ if environ is None else environ
    result = copy.deepcopy(cfg)
    for name in sorted(environ):
        if not name.startswith(WG_CONFIG_ENV_PREFIX):
            continue
        path = name[len(WG_CONFIG_ENV_PREFIX) :].split("__")
        if not all(path):
            raise ValueError(f"{name}: empty key")
        try:
            value = _parse_env_value(path[-1], environ[name])
        except ValueError as e:
            raise ValueError(f"{name}: {e}") from None
        node = result
        for part in path[:-1]:
            key = _env_key(node, part)
            if node.get(key) is None:
                node[key] = {}
            if not isinstance(node[key], dict):
                raise ValueError(f"{name}: {key} is not a map")  # noqa: TRY004
            node = node[key]
        node[_env_key(node, path[-1])] = value
    return result


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
        endpoint_name: An optional hostname to hand to Parker nodes instead of the
            address the worker announces over MQTT. The port is unaffected. Set it
            to have nodes resolve the concentrator endpoint via DNS.
    """

    weight: int
    location: str | None = None
    pop: str = ""
    id: int = 0
    endpoint_name: str | None = None

    @classmethod
    def from_dict(cls, worker_cfg: dict[str, Any]) -> "Worker":
        location = worker_cfg.get("location")
        worker_id = int(worker_cfg.get("id", 0))
        if not 0 <= worker_id <= 2**32 - 1:
            raise ValueError("Worker ID must be an unsigned 32-bit integer")
        endpoint_name = worker_cfg.get("endpoint_name")
        return cls(
            weight=int(worker_cfg["weight"]) if worker_cfg.get("weight") else 1,
            location=str(location) if location else None,
            pop=str(worker_cfg.get("pop") or ""),
            id=worker_id,
            endpoint_name=str(endpoint_name) if endpoint_name else None,
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

    def names_in_pop(self, pop: str) -> list[str]:
        """Return the names of all configured workers in a PoP, sorted."""
        return sorted(name for name, w in self._workers.items() if w.pop == pop)

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

    For compatibility with early Parker deployments, the keys of a 'cleanup' section are
    accepted as well ('legacy_stale_timeout' maps to stale_timeout), 'peer_cleanup' wins.

    Attributes:
        stale_timeout: Seconds without a handshake after which a (legacy) peer is removed.
        interval: Seconds between two cleanup runs.
        parker_stale_timeout: Seconds without a handshake after which a Parker peer is removed.
        initial_handshake_grace: Seconds a never-handshaked Parker peer is kept after it was
            provisioned or after the worker started.
    """

    stale_timeout: int = 1800
    interval: int = 300
    parker_stale_timeout: float = 300
    initial_handshake_grace: float = 600

    @classmethod
    def from_dict(
        cls, cfg: dict[str, Any], parker_cleanup: dict[str, Any] | None = None
    ) -> "PeerCleanup":
        if parker_cleanup:
            if not isinstance(parker_cleanup, dict):
                raise ValueError("cleanup must be a mapping")
            aliased = {
                "interval": parker_cleanup.get("interval"),
                "stale_timeout": parker_cleanup.get("legacy_stale_timeout"),
                "parker_stale_timeout": parker_cleanup.get("parker_stale_timeout"),
                "initial_handshake_grace": parker_cleanup.get(
                    "initial_handshake_grace"
                ),
            }
            cfg = {
                **{k: v for k, v in aliased.items() if v is not None},
                **cfg,
            }
        return cls(
            stale_timeout=int(cfg.get("stale_timeout", cls.stale_timeout)),
            interval=int(cfg.get("interval", cls.interval)),
            parker_stale_timeout=_positive_duration(
                cfg.get("parker_stale_timeout", cls.parker_stale_timeout),
                "peer_cleanup.parker_stale_timeout",
            ),
            initial_handshake_grace=_positive_duration(
                cfg.get("initial_handshake_grace", cls.initial_handshake_grace),
                "peer_cleanup.initial_handshake_grace",
            ),
        )

    def timeout_for(self, parker: bool) -> float:
        """Returns the stale timeout for the given worker mode."""
        return self.parker_stale_timeout if parker else self.stale_timeout


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
            port=int(broker_listen["port"]) if broker_listen.get("port") else None,
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

            clat_subnet: str | None = None
            length: int | None = None
            netbox_filter: dict[str, Any] | None = None
            netbox_additional_data: dict[str, Any] | None = None

        @dataclasses.dataclass
        class IPv6:
            """A representation of the 'ipv6' key in the 'prefixes' configuration.

            Attributes:
                length: The prefix size to use for the IPv6 prefix.
                netbox_filter: The filter to use when querying NetBox for an IPv6 prefix.
                netbox_additional_data: Additional data to send to NetBox when reserving an IPv6 prefix.
            """

            length: int
            netbox_filter: dict[str, Any] | None = None
            netbox_additional_data: dict[str, Any] | None = None

        ipv4: IPv4
        ipv6: IPv6

    class IPAM(Enum):
        JSON = "json"
        NETBOX = "netbox"

    @dataclasses.dataclass(frozen=True)
    class Site:
        """One site (PoP) of a multi-site Parker network.

        Attributes:
            name: The site name. It is the same as the 'pop' of its workers.
            tunnel_id: The concentrator ID of the node tunnel to this site. It
                stays the same when the node changes the pgw in the site.
            dns64: The DNS64 server of the site, sent to the nodes.
            netbox_filter: The NetBox filter for the parent prefix of the site.
            json_file: The JSON IPAM file of the site.
            capacity: The maximum number of nodes with this primary site.
                None means no limit.
            weight: The relative share of new nodes for this site.
        """

        name: str
        tunnel_id: int
        dns64: str | None = None
        netbox_filter: dict[str, Any] | None = None
        json_file: str | None = None
        capacity: int | None = None
        weight: float = 1

    @dataclasses.dataclass(frozen=True)
    class RouteGate:
        """Worker: install the /63 route of a node only with a fresh handshake.

        Attributes:
            handshake_max_age: A handshake older than this (seconds) is not fresh.
            interval: How often the worker checks the handshakes (seconds).
            route_metric: The kernel metric of the node routes. It must be lower
                than the metric of the routing daemon (BIRD default: 32), so that
                the local route wins over the iBGP route of the other pgw.
        """

        handshake_max_age: float = 180
        interval: float = 1
        route_metric: int = 16

    @dataclasses.dataclass(frozen=True)
    class PgwSelection:
        """Broker: how a node gets its preferred pgw in a site (multi-site only).

        A node keeps its pgw while the pgw is healthy. The broker moves a node
        for load reasons only when its pgw has more than
        max(rebalance_min_excess, rebalance_tolerance * target) peers above its
        weighted target in the site.

        Attributes:
            rebalance_tolerance: The relative excess above the target (0.2 = 20 %).
            rebalance_min_excess: The minimum excess in peers.
            rebalance_max_moves: The maximum number of moves in each site and window.
                0: no load moves.
            rebalance_window: The rate limit window for moves (seconds).
        """

        rebalance_tolerance: float = 0.2
        rebalance_min_excess: int = 10
        rebalance_max_moves: int = 2
        rebalance_window: float = 120

    @dataclasses.dataclass(frozen=True)
    class IPAMCache:
        """Broker: the in-memory copy of the NetBox IPAM.

        Attributes:
            snapshot_dir: The directory for the local snapshot files. None: no snapshot.
            sync_interval: How often the broker reads all prefixes from NetBox (seconds).
            reserve: The number of free prefixes per site that the broker reserves
                in NetBox. It uses them only when NetBox is not available.
            reclaim_days: Once per day, delete the prefixes of nodes that no site
                saw for this number of days. 0: off. Enable it on one broker only.
            reclaim_dry_run: Only log what the reclaim would delete.
        """

        snapshot_dir: str | None = None
        sync_interval: float = 60
        reserve: int = 32
        reclaim_days: float = 0
        reclaim_dry_run: bool = True

    enabled: bool
    xlat: bool
    prefixes: Prefixes
    ipam: IPAM
    retry_interval: int = 120
    wg_keepalive: int = 25
    sites: dict[str, Site] = dataclasses.field(default_factory=dict)
    route_gate: RouteGate | None = None
    ipam_cache: IPAMCache = dataclasses.field(default_factory=IPAMCache)
    pgw_selection: PgwSelection = dataclasses.field(default_factory=PgwSelection)
    captive_portal_urls: dict[str, str] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_dict(cls, parker_cfg: dict[str, Any]) -> "Parker":
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
        if (
            ipam == cls.IPAM.NETBOX
            and not parker_cfg.get("sites")
            and (
                "netbox_filter" not in pfx["ipv6"]
                or not isinstance(pfx["ipv6"]["netbox_filter"], dict)
            )
        ):
            raise ValueError(
                "Parker prefixes config must contain 'netbox_filter' key for 'ipv6' when using NetBox as IPAM and no 'sites'"
            )

        # TODO remove this block when non-464XLAT mode is fully supported
        if not xlat:
            raise NotImplementedError("Non-464XLAT mode is not supported yet")

        retry_interval = int(parker_cfg.get("retry_interval", cls.retry_interval))
        wg_keepalive = int(parker_cfg.get("wg_keepalive", cls.wg_keepalive))
        sites = cls._sites_from_dict(parker_cfg, ipam)

        route_gate = None
        gate_cfg = parker_cfg.get("route_gate")
        if gate_cfg is not None:
            if not isinstance(gate_cfg, dict):
                raise ValueError("parker.route_gate must be a mapping")
            route_gate = cls.RouteGate(
                handshake_max_age=_positive_duration(
                    gate_cfg.get("handshake_max_age", 180),
                    "parker.route_gate.handshake_max_age",
                ),
                interval=_positive_duration(
                    gate_cfg.get("interval", 1), "parker.route_gate.interval"
                ),
                route_metric=_route_metric(gate_cfg.get("route_metric", 16)),
            )

        cache_cfg = parker_cfg.get("ipam_cache")
        if cache_cfg is None:
            cache_cfg = {}
        if not isinstance(cache_cfg, dict):
            raise ValueError("parker.ipam_cache must be a mapping")  # noqa: TRY004
        reserve = int(cache_cfg.get("reserve", cls.IPAMCache.reserve))
        if reserve < 0:
            raise ValueError("parker.ipam_cache.reserve must not be negative")
        ipam_cache = cls.IPAMCache(
            snapshot_dir=cache_cfg.get("snapshot_dir"),
            sync_interval=_positive_duration(
                cache_cfg.get("sync_interval", cls.IPAMCache.sync_interval),
                "parker.ipam_cache.sync_interval",
            ),
            reserve=reserve,
            reclaim_days=float(cache_cfg.get("reclaim_days", 0)),
            reclaim_dry_run=cache_cfg.get("reclaim_dry_run", True) is not False,
        )
        if ipam_cache.reclaim_days < 0:
            raise ValueError("parker.ipam_cache.reclaim_days must not be negative")

        pgw_selection = cls._pgw_selection_from_dict(parker_cfg.get("pgw_selection"))

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
            sites=sites,
            route_gate=route_gate,
            ipam_cache=ipam_cache,
            pgw_selection=pgw_selection,
            captive_portal_urls=_captive_portal_urls(
                parker_cfg.get("captive_portal_urls")
            ),
        )

    @classmethod
    def _pgw_selection_from_dict(cls, sel_cfg: Any) -> "Parker.PgwSelection":
        if sel_cfg is None:
            sel_cfg = {}
        if not isinstance(sel_cfg, dict):
            raise ValueError("parker.pgw_selection must be a mapping")  # noqa: TRY004
        defaults = cls.PgwSelection()
        tolerance = float(
            sel_cfg.get("rebalance_tolerance", defaults.rebalance_tolerance)
        )
        if not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError("parker.pgw_selection.rebalance_tolerance must be >= 0")
        min_excess = int(
            sel_cfg.get("rebalance_min_excess", defaults.rebalance_min_excess)
        )
        max_moves = int(
            sel_cfg.get("rebalance_max_moves", defaults.rebalance_max_moves)
        )
        if min_excess < 0 or max_moves < 0:
            raise ValueError(
                "parker.pgw_selection.rebalance_min_excess and rebalance_max_moves must be >= 0"
            )
        return cls.PgwSelection(
            rebalance_tolerance=tolerance,
            rebalance_min_excess=min_excess,
            rebalance_max_moves=max_moves,
            rebalance_window=_positive_duration(
                sel_cfg.get("rebalance_window", defaults.rebalance_window),
                "parker.pgw_selection.rebalance_window",
            ),
        )

    @classmethod
    def _sites_from_dict(
        cls, parker_cfg: dict[str, Any], ipam: "Parker.IPAM"
    ) -> dict[str, "Parker.Site"]:
        sites_cfg = parker_cfg.get("sites") or {}
        if not isinstance(sites_cfg, dict):
            raise ValueError("parker.sites must be a mapping")  # noqa: TRY004
        sites: dict[str, Parker.Site] = {}
        for name, site_cfg in sites_cfg.items():
            if not isinstance(site_cfg, dict) or "tunnel_id" not in site_cfg:
                raise ValueError(f"parker.sites.{name} must set tunnel_id")
            netbox_filter = site_cfg.get("netbox_filter")
            json_file = site_cfg.get("json_file")
            if ipam == cls.IPAM.NETBOX and not isinstance(netbox_filter, dict):
                raise ValueError(f"parker.sites.{name} must set netbox_filter")
            if ipam == cls.IPAM.JSON and not json_file:
                raise ValueError(f"parker.sites.{name} must set json_file")
            capacity = site_cfg.get("capacity")
            if capacity is not None and (
                isinstance(capacity, bool)
                or not isinstance(capacity, int)
                or capacity < 0
            ):
                raise ValueError(
                    f"parker.sites.{name}.capacity must be an integer >= 0"
                )
            weight = site_cfg.get("weight", 1)
            if (
                isinstance(weight, bool)
                or not isinstance(weight, (int, float))
                or not math.isfinite(weight)
                or weight <= 0
            ):
                raise ValueError(f"parker.sites.{name}.weight must be a number > 0")
            sites[str(name)] = cls.Site(
                name=str(name),
                tunnel_id=int(site_cfg["tunnel_id"]),
                dns64=site_cfg.get("dns64"),
                netbox_filter=netbox_filter,
                json_file=json_file,
                capacity=capacity,
                weight=float(weight),
            )
        tunnel_ids = [site.tunnel_id for site in sites.values()]
        if len(set(tunnel_ids)) != len(tunnel_ids):
            raise ValueError("parker.sites tunnel_id values must be unique")
        return sites


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
    def from_dict(cls, netbox_cfg: dict[str, str]) -> "Netbox":
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
            peer_cleanup=PeerCleanup.from_dict(
                cfg.get("peer_cleanup") or {}, cfg.get("cleanup")
            ),
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
        try:
            cfg_contents = fetch_config_from_disk()
        except ConfigFileNotFoundError:
            if not env_overrides_present():
                raise
            cfg_contents = "{}"
        try:
            config = yaml.safe_load(cfg_contents)
        except yaml.YAMLError as e:
            print(f"Failed to load YAML file: {e}")
            sys.exit(1)
        if config is None:
            config = {}
        if isinstance(config, dict) and env_overrides_present():
            try:
                config = apply_env_overrides(config)
            except ValueError as e:
                print(f"Failed to apply environment overrides: {e}")
                sys.exit(1)
        try:
            config = Config.from_dict(config)
        except (KeyError, TypeError, AttributeError, ValueError) as e:
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
