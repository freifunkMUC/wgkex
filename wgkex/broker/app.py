"""wgkex broker"""

import dataclasses
import ipaddress
import json
import os
import random
import re
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt_client
import paho.mqtt.enums
from flask import Flask, Response, abort, render_template, request
from flask.app import Flask as Flask_app
from flask_mqtt import Mqtt
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from waitress import serve

from wgkex.broker import observability
from wgkex.broker.assignments import Assignment, AssignmentHistory
from wgkex.broker.ipam import ParkerIPAM
from wgkex.broker.ipam_cache import IPAMCache, reclaim
from wgkex.broker.ipam_json import JSONFileIPAM
from wgkex.broker.ipam_netbox import NetboxIPAM
from wgkex.broker.metrics import WorkerMetricsCollection, WorkerResult
from wgkex.broker.observability import (
    ASSIGNMENTS,
    KEY_EXCHANGE_REQUESTS,
    MQTT_MESSAGE_ERRORS,
    PGW_SELECTIONS,
    REBALANCE_HINT_EXCESS,
    REBALANCE_HINTS,
    SHARED_ASSIGNMENTS,
)
from wgkex.broker.parker import ParkerQuery, ParkerResponse
from wgkex.broker.pgw_select import PgwSelector
from wgkex.broker.ratelimit import RateLimiter, client_address
from wgkex.broker.signer import sign_response
from wgkex.common import logger
from wgkex.common.keylist import KeyList
from wgkex.common.mqtt import (
    STATUS_DRAINING,
    TOPIC_BROKER_ASSIGNMENT,
    TOPIC_BROKER_IPAM,
    TOPIC_WG_KEY,
    TOPIC_WORKER_REBALANCE,
    TOPIC_WORKER_REMOVE_PEER,
    TOPIC_WORKER_STATUS,
    TOPIC_WORKER_WG_DATA,
    WG_KEY_ALL_WORKERS,
    MQTTTopics,
)
from wgkex.common.subscriptions import SubscriptionGuard
from wgkex.common.utils import (
    WG_PUBKEY_PATTERN,  # noqa: F401 re-exported for compatibility
    is_valid_domain,
    is_valid_wg_pubkey,
)
from wgkex.config import config

LOCATION_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


@dataclasses.dataclass
class KeyExchange:
    """A key exchange message.

    Attributes:
        public_key: The public key for this exchange.
        domain: The domain for this exchange.
        location: Optional location of the client, used to prefer workers in that location.
    """

    public_key: str
    domain: str
    location: str | None = None

    @classmethod
    def from_dict(cls, msg: dict) -> "KeyExchange":
        """Creates a new KeyExchange message from dict.

        Arguments:
            msg: The message to convert.
        Returns:
            A KeyExchange object.
        """
        public_key = is_valid_wg_pubkey(msg.get("public_key"))
        domain = str(msg.get("domain"))
        if not is_valid_domain(domain):
            raise ValueError(f"Domain {domain} not in configured domains.")
        location = msg.get("location")
        # Optional and only a preference, so invalid values are ignored instead of rejected
        if not isinstance(location, str) or not LOCATION_PATTERN.match(location):
            location = None
        return cls(public_key=public_key, domain=domain, location=location)


def _fetch_app_config() -> Flask_app:
    """Creates the Flask app from configuration.

    Returns:
        A created Flask app.
    """
    app = Flask(__name__)
    mqtt_cfg = config.get_config().mqtt
    app.config["MQTT_BROKER_URL"] = mqtt_cfg.broker_url
    app.config["MQTT_BROKER_PORT"] = mqtt_cfg.broker_port
    app.config["MQTT_USERNAME"] = mqtt_cfg.username
    app.config["MQTT_PASSWORD"] = mqtt_cfg.password
    app.config["MQTT_KEEPALIVE"] = mqtt_cfg.keepalive
    app.config["MQTT_TLS_ENABLED"] = mqtt_cfg.tls
    return app


def _new_ipam(
    name: str = "default",
    netbox_filter: dict[str, Any] | None = None,
    json_file: str | None = None,
) -> ParkerIPAM:
    parker_cfg = config.get_config().parker
    ipam_type = parker_cfg.ipam
    match ipam_type:
        case config.Parker.IPAM.JSON:
            return JSONFileIPAM(json_file) if json_file else JSONFileIPAM()
        case config.Parker.IPAM.NETBOX:
            netbox_cfg = config.get_config().netbox
            if netbox_cfg is None:
                raise ValueError(
                    "Parker is enabled with NetBox IPAM, but no netbox config is set in the config file"
                )

            cache_cfg = parker_cfg.ipam_cache
            snapshot = (
                os.path.join(cache_cfg.snapshot_dir, f"ipam-{name}.json")
                if cache_cfg.snapshot_dir
                else None
            )
            cache = IPAMCache(
                name,
                lambda: NetboxIPAM(
                    api_url=netbox_cfg.url,
                    token=netbox_cfg.api_key,
                    xlat=parker_cfg.xlat,
                    v6_filter=netbox_filter,
                ),
                # Must be stable over restarts: it marks the reserved prefixes.
                _load_balancing_cfg.broker_id or socket.gethostname(),
                notify=_publish_ipam,
                snapshot_file=snapshot,
                reserve=cache_cfg.reserve,
                prefix_length=parker_cfg.prefixes.ipv6.length,
                sync_interval=cache_cfg.sync_interval,
            )
            ipam_caches[name] = cache
            return cache
        case _:
            raise NotImplementedError(f"Invalid IPAM type {ipam_type}")


def _load_parker_ipam() -> ParkerIPAM | None:
    parker_cfg = config.get_config().parker
    if not parker_cfg.enabled or parker_cfg.sites:
        return None
    return _new_ipam()


def _load_site_ipams() -> dict[str, ParkerIPAM]:
    """Load one IPAM per site. Each site has its own parent prefix."""
    parker_cfg = config.get_config().parker
    if not parker_cfg.enabled:
        return {}
    return {
        name: _new_ipam(name, site.netbox_filter, site.json_file)
        for name, site in parker_cfg.sites.items()
    }


# Seconds after the start until the first prefix reclaim, then once per day.
_RECLAIM_DELAY = 300
_RECLAIM_INTERVAL = 86400


def reclaim_loop(exit_event: threading.Event) -> None:
    """Deletes the prefixes of nodes that were not seen for parker.ipam_cache.reclaim_days."""
    cache_cfg = config.get_config().parker.ipam_cache
    delay = _RECLAIM_DELAY
    while not exit_event.wait(delay):
        delay = _RECLAIM_INTERVAL
        try:
            reclaim(
                ipam_caches,
                cache_cfg.reclaim_days * 86400,
                cache_cfg.reclaim_dry_run,
                time.time(),
            )
        except Exception as error:  # noqa: BLE001
            logger.error("Prefix reclaim failed: %s", error, exc_info=error)


app = _fetch_app_config()
# Connected via init_app() at the end of this module, after all handlers are registered.
mqtt = Mqtt()
subscriptions = SubscriptionGuard()
_load_balancing_cfg = config.get_config().load_balancing
worker_metrics = WorkerMetricsCollection(
    pending_ttl=_load_balancing_cfg.pending_assignment_ttl,
    pending_settle_time=_load_balancing_cfg.pending_assignment_settle_time,
    metrics_max_age=_load_balancing_cfg.metrics_max_age,
)
worker_data: dict[tuple[str, str], dict] = {}
parker_worker_metrics = WorkerMetricsCollection(
    pending_ttl=_load_balancing_cfg.pending_assignment_ttl,
    pending_settle_time=_load_balancing_cfg.pending_assignment_settle_time,
    metrics_max_age=_load_balancing_cfg.metrics_max_age,
)
parker_worker_data: dict[str, dict] = {}
assignment_history = AssignmentHistory(
    ttl=_load_balancing_cfg.previous_assignment_ttl,
    max_entries=_load_balancing_cfg.previous_assignment_max_entries,
)
observability.register_worker_metrics(worker_metrics, parker_worker_metrics)
broker_id = (
    _load_balancing_cfg.broker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
)


def _create_rate_limiter() -> RateLimiter:
    cfg = config.get_config().rate_limit
    return RateLimiter(
        per_client_requests=cfg.per_client_requests,
        per_client_period=cfg.per_client_period,
        global_requests=cfg.global_requests,
        global_period=cfg.global_period,
        ipv6_prefix_length=cfg.ipv6_prefix_length,
        max_clients=cfg.max_tracked_clients,
    )


def _on_blocklist_change(added: set[str], removed: set[str]) -> None:
    """Removes newly blocked keys from all workers."""
    logger.info(f"Blocklist reloaded: {len(added)} keys added, {len(removed)} removed")
    for key in sorted(added):
        for worker, domain in sorted(worker_data):
            mqtt.publish(
                TOPIC_WORKER_REMOVE_PEER.format(worker=worker, domain=domain),
                key,
                qos=1,
            )


def _create_blocklist() -> KeyList:
    cfg = config.get_config().key_lists
    return KeyList(
        "blocklist",
        keys=cfg.blocklist,
        file=cfg.blocklist_file,
        check_interval=cfg.reload_interval,
        on_change=_on_blocklist_change,
    )


# Seconds to wait for the PUBACK of an IPAM message.
_IPAM_PUBLISH_TIMEOUT = 2


def _publish_ipam(site: str, pubkey: str, entry: dict[str, Any]) -> None:
    """Send an IPAM change to the other brokers and wait for the PUBACK of EMQX."""
    if not mqtt.connected:
        logger.warning(
            "MQTT is not connected, the other brokers learn %s later", pubkey
        )
        return
    payload = json.dumps({"site": site, "pubkey": pubkey, "entry": entry})
    info = mqtt.client.publish(
        TOPIC_BROKER_IPAM.format(broker=broker_id), payload, qos=1
    )
    info.wait_for_publish(timeout=_IPAM_PUBLISH_TIMEOUT)


rate_limiter = _create_rate_limiter()
blocklist = _create_blocklist()
# Site -> IPAM cache, only with the NetBox IPAM.
ipam_caches: dict[str, IPAMCache] = {}
ipam: ParkerIPAM | None = _load_parker_ipam()
site_ipams: dict[str, ParkerIPAM] = _load_site_ipams()
# Public key -> (worker, receive time) of the last fresh handshake that a
# worker reported. Used to accept switch reports from nodes.
handshakes: dict[str, tuple[str, float]] = {}
# (site, public key) -> (worker, receive time) of the last fresh handshake in
# each site. A multi-site node has one tunnel per site.
site_handshakes: dict[tuple[str, str], tuple[str, float]] = {}
pgw_selector = PgwSelector()


def _blocked_response(api: str, key: str) -> tuple[dict, int]:
    logger.warning(f"Rejected blocked key {key} on the {api} API")
    KEY_EXCHANGE_REQUESTS.labels(api, "blocked").inc()
    return {"error": {"message": "key is blocked"}}, 403


def _client_address() -> str | None:
    """The client address of the current request, honoring trusted reverse proxies."""
    return client_address(
        request.remote_addr,
        request.headers.get("X-Forwarded-For"),
        config.get_config().broker.trusted_proxies,
    )


def _client_log_suffix(address: str | None) -> str:
    return f", Client: {address}" if config.get_config().broker.log_client_ip else ""


def _is_rate_limited(api: str, key: str, address: str | None) -> bool:
    """Takes a rate limit token for the current request, returns True if the request is limited."""
    allowed, limit = rate_limiter.allow(address)
    if allowed:
        return False
    logger.info(
        f"Rate limited ({limit}) {api} request from {address} for key {key}, not publishing it"
    )
    KEY_EXCHANGE_REQUESTS.labels(api, "rate_limited").inc()
    return True


@app.route("/", methods=["GET"])
def index() -> str:
    """Returns main page"""
    return render_template("index.html")


@app.route("/metrics", methods=["GET"])
def prometheus_metrics() -> Response:
    """Returns Prometheus metrics, unless disabled in the configuration."""
    if not config.get_config().metrics.broker_endpoint:
        abort(404)
    return Response(
        generate_latest(observability.REGISTRY), mimetype=CONTENT_TYPE_LATEST
    )


@app.route("/api/v1/wg/key/exchange", methods=["POST"])
def wg_api_v1_key_exchange() -> tuple[Response | dict, int]:
    """Retrieves a new key and validates.
    Returns:
        Status message.
    """
    try:
        data = KeyExchange.from_dict(request.get_json(force=True))
    except Exception as ex:  # noqa: BLE001
        logger.error(
            "Exception occurred in /api/v1/wg/key/exchange: %s", ex, exc_info=True
        )
        KEY_EXCHANGE_REQUESTS.labels("v1", "invalid").inc()
        return {
            "error": {
                "message": "An internal error has occurred. Please try again later."
            }
        }, 400

    key = data.public_key
    domain = data.domain
    address = _client_address()
    logger.info(
        f"wg_api_v1_key_exchange: Domain: {domain}, Key:{key}{_client_log_suffix(address)}"
    )
    if key in blocklist:
        return _blocked_response("v1", key)
    if _is_rate_limited("v1", key, address):
        # Same response as usual, but don't publish the key (again)
        return {"Message": "OK"}, 200

    # v1 clients pick a random gateway themselves, so every worker needs the key
    publish_key(domain, key, WG_KEY_ALL_WORKERS)
    KEY_EXCHANGE_REQUESTS.labels("v1", "ok").inc()
    return {"Message": "OK"}, 200


@app.route("/api/v2/wg/key/exchange", methods=["POST"])
def wg_api_v2_key_exchange() -> tuple[Response | dict, int]:
    """Retrieves a new key, validates it and responds with a worker/gateway the client should connect to.

    Returns:
        Status message, Endpoint with address/domain, port pubic key and link address.
    """
    try:
        data = KeyExchange.from_dict(request.get_json(force=True))
    except Exception as ex:  # noqa: BLE001
        logger.error(
            "Exception occurred in /api/v2/wg/key/exchange: %s", ex, exc_info=True
        )
        KEY_EXCHANGE_REQUESTS.labels("v2", "invalid").inc()
        return {
            "error": {
                "message": "An internal error has occurred. Please try again later."
            }
        }, 400

    key = data.public_key
    domain = data.domain
    address = _client_address()
    logger.info(
        f"wg_api_v2_key_exchange: Domain: {domain}, Key:{key}, Location: {data.location}"
        f"{_client_log_suffix(address)}"
    )
    if key in blocklist:
        return _blocked_response("v2", key)
    limited = _is_rate_limited("v2", key, address)

    # Only consider workers whose endpoint data we know, so we can always answer with a valid endpoint
    def eligible(worker: str) -> bool:
        return (worker, domain) in worker_data

    if limited:
        # Answer with the worker the key was already sent to, so the client connects there
        sticky = _current_worker(key, domain, eligible)
        if sticky is not None:
            return {"Endpoint": _endpoint(worker_data[(sticky, domain)])}, 200

    lb_cfg = config.get_config().load_balancing
    best_worker, diff, current_peers = worker_metrics.get_best_worker(
        domain,
        eligible=eligible,
        location=data.location,
        location_tolerance=lb_cfg.location_tolerance,
        location_min_excess=lb_cfg.location_min_excess_peers,
    )
    if best_worker is None:
        logger.warning(f"No worker online for domain {domain}")
        # Like before, so the key is present on every worker once the client falls back to v1
        if not limited:
            publish_key(domain, key, WG_KEY_ALL_WORKERS)
        KEY_EXCHANGE_REQUESTS.labels("v2", "no_worker").inc()
        return {
            "error": {
                "message": "no gateway online for this domain, please check the domain value and try again later"
            }
        }, 400

    w_data = worker_data.get((best_worker, domain), None)
    if w_data is None:
        logger.error(f"Couldn't get worker endpoint data for {best_worker}/{domain}")
        if not limited:
            publish_key(domain, key, WG_KEY_ALL_WORKERS)
        KEY_EXCHANGE_REQUESTS.labels("v2", "no_worker_data").inc()
        return {"error": {"message": "could not get gateway data"}}, 500

    if limited:
        # The key is not published, so don't count it as an assignment either
        return {"Endpoint": _endpoint(w_data)}, 200

    # Count the peer for this worker until the worker metrics reflect it
    worker_metrics.record_assignment(best_worker, domain, key)
    ASSIGNMENTS.labels(best_worker, domain).inc()
    logger.debug(
        f"Chose worker {best_worker} with {current_peers} effective peers ({diff:.1f} to target)"
    )
    if lb_cfg.targeted_publish:
        publish_key(domain, key, best_worker)
    else:
        publish_key(domain, key, WG_KEY_ALL_WORKERS)
    if lb_cfg.share_pending_assignments:
        publish_shared_assignment(best_worker, domain, key)

    KEY_EXCHANGE_REQUESTS.labels("v2", "ok").inc()
    return {"Endpoint": _endpoint(w_data)}, 200


def _endpoint(w_data: dict) -> dict:
    """Builds the v2 API endpoint response from worker data. Port must stay a string."""
    return {
        "Address": w_data.get("ExternalAddress"),
        "Port": str(w_data.get("Port")),
        "AllowedIPs": [w_data.get("LinkAddress")],
        "PublicKey": w_data.get("PublicKey"),
    }


def _current_worker(key: str, domain: str, eligible) -> str | None:
    """Returns the online worker a key is currently assigned to in a domain, if any."""
    worker = worker_metrics.get_pending_worker(domain, key)
    if worker is None:
        previous = assignment_history.get(key)
        if previous is not None and previous.domain == domain:
            worker = previous.worker
    if worker is None or worker == WG_KEY_ALL_WORKERS or not eligible(worker):
        return None
    if (
        not worker_metrics.is_online(worker, domain)
        or worker_metrics.get(worker).draining
    ):
        return None
    return worker


def publish_shared_assignment(worker: str, domain: str, key: str) -> None:
    """Tells other broker instances about an assignment, so they count it as pending."""
    mqtt.publish(
        TOPIC_BROKER_ASSIGNMENT.format(broker=broker_id),
        json.dumps({"worker": worker, "domain": domain, "key": key}),
        qos=0,
        retain=False,
    )


def publish_key(domain: str, key: str, worker: str) -> None:
    """Publishes a key to a worker (or all workers) and removes it from its previous worker(s)."""
    mqtt.publish(TOPIC_WG_KEY.format(domain=domain, worker=worker), key, qos=1)
    previous = assignment_history.record(key, domain, worker)
    if worker == WG_KEY_ALL_WORKERS or previous is None:
        return
    for prev_worker in _previous_workers(previous, domain, worker):
        logger.info(
            f"Key {key} moved to {worker}/{domain}, removing it from {prev_worker}/{previous.domain}"
        )
        mqtt.publish(
            TOPIC_WORKER_REMOVE_PEER.format(worker=prev_worker, domain=previous.domain),
            key,
            qos=1,
        )


def _previous_workers(previous: Assignment, domain: str, worker: str) -> list[str]:
    """Returns the workers a key has to be removed from, after it was assigned to worker/domain."""
    if previous.worker == WG_KEY_ALL_WORKERS:
        candidates = sorted(w for (w, d) in list(worker_data) if d == previous.domain)
    else:
        candidates = [previous.worker]
    return [w for w in candidates if not (w == worker and previous.domain == domain)]


@app.route("/api/v3/wg/key/exchange", methods=["GET"])
@app.route("/api/v3/config", methods=["GET"])
@app.route("/config", methods=["GET"])
def wg_api_v3_key_exchange() -> tuple[Response | dict, int]:
    if not config.get_config().parker.enabled or (ipam is None and not site_ipams):
        KEY_EXCHANGE_REQUESTS.labels("v3", "disabled").inc()
        return {
            "error": {"message": "Parker support is not enabled on this broker"}
        }, 400

    try:
        req_data = ParkerQuery.from_dict(request.args)
    except Exception as ex:  # noqa: BLE001
        logger.warning(
            "Couldn't parse client query in /api/v3/wg/key/exchange: %s",
            ex,
            exc_info=True,
        )
        KEY_EXCHANGE_REQUESTS.labels("v3", "invalid").inc()
        return {
            "error": {
                "message": "Invalid data received. Please check your request and try again."
            }
        }, 400

    address = _client_address()
    logger.info(
        f"wg_api_v3_key_exchange: Key:{req_data.pubkey}{_client_log_suffix(address)}"
    )
    if req_data.pubkey in blocklist:
        return _blocked_response("v3", req_data.pubkey)
    # Parker nodes have no older API to fall back to and poll this endpoint
    # periodically, so a rate-limited request is simply rejected and retried later.
    if _is_rate_limited("v3", req_data.pubkey, address):
        return {"error": {"message": "too many requests"}}, 429

    try:
        if config.get_config().parker.sites:
            data = _multisite_response(req_data)
        else:
            data = _single_site_response(req_data)
        return _signed_response(data)
    except _ApiError as error:
        KEY_EXCHANGE_REQUESTS.labels("v3", error.reason).inc()
        return {"error": {"message": error.message}}, error.status


class _ApiError(Exception):
    def __init__(self, message: str, status: int, reason: str) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        # Label of the KEY_EXCHANGE_REQUESTS metric
        self.reason = reason


def _allocate_range6(
    site_ipam: ParkerIPAM, pubkey: str
) -> tuple[ipaddress.IPv6Network, list[str]]:
    """Fetch or allocate the IPv6 range of a node.

    Returns:
        The range and the concentrators that were last selected for the node.
    """
    try:
        _, full_range6, old_selected_concentrators = site_ipam.get_or_allocate_prefix(
            pubkey,
            not config.get_config().parker.xlat,
            True,
            config.get_config().parker.prefixes.ipv4.length or 0,
            config.get_config().parker.prefixes.ipv6.length,
        )
    except Exception as ex:
        logger.error(
            "Exception occurred while fetching/allocating IPv6 prefix for public key %s: %s",
            pubkey,
            ex,
            exc_info=True,
        )
        raise _ApiError(
            "Internal error while allocating IP range. Please try again later.",
            500,
            "ipam_error",
        ) from ex

    if full_range6 is None:
        raise _ApiError(
            "No IPv6 range available for this public key. Please try again later.",
            500,
            "ipam_error",
        )
    if full_range6.prefixlen > 63:
        logger.error(
            "Allocated IPv6 range %s is too small for Parker's two /64 networks",
            full_range6,
        )
        raise _ApiError(
            "Allocated IPv6 range is invalid. Please try again later.",
            500,
            "ipam_error",
        )
    expected_length = config.get_config().parker.prefixes.ipv6.length
    if full_range6.prefixlen != expected_length:
        logger.warning(
            "IPv6 prefix %s for public key %s has length /%s, but /%s is configured. "
            "Fix or delete the prefix in the IPAM.",
            full_range6,
            pubkey,
            full_range6.prefixlen,
            expected_length,
        )
    return full_range6, old_selected_concentrators


def _node_addresses(full_range6: ipaddress.IPv6Network) -> dict[str, str]:
    ranges = full_range6.subnets(new_prefix=64)
    range6 = next(ranges)
    xlat_range6 = next(ranges)
    return {
        "range6": str(range6),
        "xlat_range6": str(xlat_range6),
        "address6": str(range6.network_address + 1),
    }


def _concentrator(worker_name: str, concentrator_id: int) -> dict[str, Any]:
    w_data = parker_worker_data.get(worker_name, None)
    if w_data is None:
        logger.error(f"Couldn't get worker endpoint data for {worker_name}")
        raise _ApiError("could not get gateway data", 500, "no_worker_data")

    # An endpoint_name in the config replaces the address the worker announced,
    # leaving it to the nodes to resolve the concentrator's name.
    worker_cfg = config.get_config().workers.get(worker_name)
    endpoint_host = str(w_data.get("ExternalAddress"))
    if worker_cfg is not None and worker_cfg.endpoint_name:
        endpoint_host = worker_cfg.endpoint_name

    return {
        "address4": "10.0.0.1",  # TODO fetch real concentrator address4 from worker status
        "address6": w_data.get("LinkAddress"),
        "endpoint": join_host_port(endpoint_host, str(w_data.get("Port"))),
        "pubkey": w_data.get("PublicKey"),  # type: ignore
        "id": concentrator_id,
    }


def _count_new_peer(
    worker_name: str, pubkey: str, old_selected_concentrators: list[str]
) -> None:
    """Count a newly selected concentrator as pending until the worker metrics reflect it,
    like v2 assignments. Not for re-confirmed ones, so periodic polls don't inflate the load.
    The pending key includes the PoP, because a node has one concentrator per PoP.
    """
    if worker_name in old_selected_concentrators:
        return
    worker_cfg = config.get_config().workers.get(worker_name)
    pop = worker_cfg.pop if worker_cfg is not None else ""
    parker_worker_metrics.record_assignment(worker_name, "parker", f"{pop}/{pubkey}")
    ASSIGNMENTS.labels(worker_name, "parker").inc()


def _provision(
    pubkey: str,
    full_range6: ipaddress.IPv6Network,
    worker_names: list[str],
    old_selected_concentrators: list[str],
) -> None:
    # Provision the peer only on the given workers. A broadcast here would
    # (re-)install the peer and its prefix routes on every worker, and any
    # worker that then announces the prefix without holding a live session
    # blackholes part of the ECMP flows.
    mqtt_data = {
        "PublicKey": pubkey,
        "Range6": str(full_range6),
        "Keepalive": None,  # TODO make configurable
    }
    for worker_name in worker_names:
        mqtt.publish(
            MQTTTopics.TOPIC_PARKER_WG_PEER.format(worker=worker_name),
            json.dumps(mqtt_data).encode("utf-8"),
            qos=1,
        )

    # A de-selected worker would keep the node's peer and prefix route until
    # its stale-peer sweep runs, up to a full cleanup interval. Tell it to
    # drop the peer right away instead. A worker that predates the Remove
    # flag reads this as the re-provisioning it always received - no worse
    # than before.
    for previous_name in old_selected_concentrators:
        if previous_name in worker_names:
            continue
        mqtt.publish(
            MQTTTopics.TOPIC_PARKER_WG_PEER.format(worker=previous_name),
            json.dumps({**mqtt_data, "Remove": True}).encode("utf-8"),
            qos=1,
        )


def _base_response(req_data: ParkerQuery, addresses: dict[str, str], concentrators):
    return dataclasses.asdict(
        ParkerResponse(
            nonce=req_data.nonce,
            time=int(datetime.now(tz=timezone.utc).timestamp()),
            id=req_data.pubkey,
            mtu=min(req_data.v6mtu, 1375),
            concentrators=concentrators,
            **addresses,
        )
    )


def _single_site_response(req_data: ParkerQuery) -> dict[str, Any]:
    assert ipam is not None
    full_range6, old_selected_concentrators = _allocate_range6(ipam, req_data.pubkey)

    logger.info(f"wg_api_v3_key_exchange: Key:{req_data.pubkey}")

    selected_workers = parker_worker_metrics.get_best_workers(
        "parker", old_selected_concentrators, require_configured=True
    )

    if len(selected_workers) == 0:
        logger.error("No worker online for Parker network")
        fallback_candidates = [
            name
            for name in parker_worker_data
            if config.get_config().workers.get(name) is not None
            and parker_worker_metrics.get(name).online
        ]
        if not fallback_candidates:
            raise _ApiError(
                "no gateway online, please try again later", 400, "no_worker"
            )
        fallback_name = random.choice(fallback_candidates)
        fallback_cfg = config.get_config().workers.get(fallback_name)
        selected_workers.append(
            WorkerResult(
                name=fallback_name,
                id=fallback_cfg.id,
                diff=0,
                peers=0,
                target=0,
            )
        )

    concentrators = []
    for worker in selected_workers:
        _count_new_peer(worker.name, req_data.pubkey, old_selected_concentrators)
        logger.debug(
            f"Chose worker {worker.name} with {worker.peers} connected clients ({worker.diff})"
        )
        concentrators.append(_concentrator(worker.name, worker.id))

    selected_names = [worker.name for worker in selected_workers]
    _provision(req_data.pubkey, full_range6, selected_names, old_selected_concentrators)
    ipam.update_prefix(
        req_data.pubkey, not config.get_config().parker.xlat, True, selected_names
    )
    return _base_response(req_data, _node_addresses(full_range6), concentrators)


def _online_pgws(site: str) -> list[str]:
    return [
        worker
        for worker in config.get_config().workers.names_in_pop(site)
        if worker in parker_worker_data and parker_worker_metrics.get(worker).online
    ]


def _select_primary_site(
    pubkey: str, preferred: str | None
) -> tuple[str | None, str, str]:
    """Select the primary site of a node.

    The broker stores a new primary site only in these cases:
    1. The node prefers another site that is online and has capacity.
    2. The node has no stored site, or the stored site is full or removed.
       Then the broker stores the online site with the lowest load
       (nodes / weight) that has capacity.

    When the stored site has no online pgw, the node gets another site for this
    response only. When the stored site is online again, the node gets it again.

    Returns:
        The stored primary site (None if there is none), the primary site to
        store and the site for this response.
    """
    sites = config.get_config().parker.sites
    try:
        stored = next(
            (name for name in sites if site_ipams[name].is_primary(pubkey)), None
        )
    except Exception as error:
        logger.error("Could not read the primary site of %s", pubkey, exc_info=error)
        raise _ApiError(
            "Internal error while reading the primary site. Please try again later.",
            500,
            "ipam_error",
        ) from error

    def has_capacity(name: str, new_node: bool) -> bool:
        capacity = sites[name].capacity
        # A node that already has this site is in the count.
        return (
            capacity is None
            or site_ipams[name].count_primary() + int(new_node) <= capacity
        )

    def lowest_load(exclude: str | None) -> str:
        others = [name for name in sites if name != exclude]
        online = [name for name in others if _online_pgws(name)]
        candidates = (
            [name for name in online if has_capacity(name, True)]
            or online
            or others
            or list(sites)
        )
        return min(
            candidates,
            key=lambda name: site_ipams[name].count_primary() / sites[name].weight,
        )

    if (
        preferred in sites
        and preferred != stored
        and _online_pgws(preferred)
        and has_capacity(preferred, True)
    ):
        return stored, preferred, preferred
    if stored is not None and has_capacity(stored, False):
        if _online_pgws(stored):
            return stored, stored, stored
        return stored, stored, lowest_load(stored)
    target = lowest_load(stored)
    return stored, target, target


def _select_site_pgw(
    site: str, pubkey: str, pgws: list[str], stored: list[str], standby: bool
) -> str:
    """Select the preferred pgw of a node in a site, see PgwSelector."""
    sel_cfg = config.get_config().parker.pgw_selection
    pgw_selector.tolerance = sel_cfg.rebalance_tolerance
    pgw_selector.min_excess = sel_cfg.rebalance_min_excess
    pgw_selector.max_moves = sel_cfg.rebalance_max_moves
    pgw_selector.window = sel_cfg.rebalance_window

    healthy = [w for w in pgws if parker_worker_metrics.is_online(w, "parker")]
    not_draining = [w for w in healthy if not parker_worker_metrics.is_draining(w)]
    healthy = not_draining or healthy or pgws

    reported = site_handshakes.get((site, pubkey))
    handshake_pgw = None
    if reported is not None and time.monotonic() - reported[1] <= _handshake_max_age():
        handshake_pgw = reported[0]
    stored_pgw = next(
        (w for w in stored if w in config.get_config().workers.names_in_pop(site)), None
    )

    selection = pgw_selector.select(
        site,
        parker_worker_metrics.site_loads(healthy),
        handshake_pgw,
        stored_pgw,
        standby,
    )
    assert selection is not None
    if selection.pgw != stored_pgw:
        logger.info(
            "Preferred pgw of %s in site %s: %s -> %s (%s)",
            pubkey,
            site,
            stored_pgw,
            selection.pgw,
            selection.reason,
        )
    PGW_SELECTIONS.labels(site, selection.reason).inc()
    return selection.pgw


def _multisite_response(req_data: ParkerQuery) -> dict[str, Any]:
    """Build the response for a multi-site (active/standby) network.

    The node gets one range and one tunnel per site. Its peer goes to all pgws
    of each site: the preferred pgw is active, the others are standby.
    """
    parker_cfg = config.get_config().parker
    workers_cfg = config.get_config().workers
    stored_primary, target, primary = _select_primary_site(
        req_data.pubkey, req_data.preferred_site
    )
    if target != primary:
        logger.warning(
            "Primary site %s of %s is offline, use %s for this response",
            target,
            req_data.pubkey,
            primary,
        )
    if stored_primary != target:
        logger.info(
            "Primary site of %s changes from %s to %s",
            req_data.pubkey,
            stored_primary,
            target,
        )
    site_names = list(parker_cfg.sites)

    logger.info(
        "wg_api_v3_key_exchange: Key:%s primary_site:%s",
        req_data.pubkey,
        primary,
    )

    allocations = {
        name: _allocate_range6(site_ipams[name], req_data.pubkey) for name in site_names
    }
    sites: dict[str, dict[str, Any]] = {}
    concentrators = []
    for name in site_names:
        site = parker_cfg.sites[name]
        full_range6, old_selected_concentrators = allocations[name]
        pgws = _online_pgws(name)
        if not pgws:
            logger.warning("No pgw online in site %s", name)
            continue
        preferred = _select_site_pgw(
            name,
            req_data.pubkey,
            pgws,
            old_selected_concentrators,
            standby=name != primary,
        )
        _count_new_peer(preferred, req_data.pubkey, old_selected_concentrators)
        _provision(
            req_data.pubkey,
            full_range6,
            pgws,
            old_selected_concentrators,
        )
        site_ipams[name].update_prefix(
            req_data.pubkey,
            not parker_cfg.xlat,
            True,
            [preferred],
            primary=name == target,
        )

        concentrator = _concentrator(preferred, workers_cfg.get(preferred).id)
        concentrator["id"] = site.tunnel_id
        concentrators.append(concentrator)
        sites[name] = {
            **_node_addresses(full_range6),
            "dns64": site.dns64,
            "concentrator": site.tunnel_id,
            "pgws": [
                {
                    **_concentrator(worker, workers_cfg.get(worker).id),
                    "name": worker,
                    "preferred": worker == preferred,
                }
                for worker in pgws
            ],
        }

    if not sites:
        logger.error("No worker online for Parker network")
        raise _ApiError("no gateway online, please try again later", 400, "no_worker")
    if primary not in sites:
        primary = next(iter(sites))

    addresses = {
        key: sites[primary][key] for key in ("range6", "xlat_range6", "address6")
    }
    data = _base_response(req_data, addresses, concentrators)
    data["primary_site"] = primary
    data["sites"] = sites
    return data


def _signed_response(data: dict[str, Any]) -> tuple[Response | dict, int]:
    encoded = json.dumps(data).encode("utf-8") + b"\n"
    if config.get_config().broker_signing_key is None:
        logger.error(
            "Parker is enabled, but no broker_signing_key is set in the config file. Can't respond to key exchange."
        )
        raise _ApiError(
            "Internal signature error. Please try again later.", 500, "signing_error"
        )

    signature: bytes = sign_response(encoded)

    KEY_EXCHANGE_REQUESTS.labels("v3", "ok").inc()
    return (
        Response(
            response=encoded + signature,
            mimetype="text/plain",
        ),
        200,
    )


def _handshake_max_age() -> float:
    gate = config.get_config().parker.route_gate or config.Parker.RouteGate()
    return gate.handshake_max_age


@app.route("/api/v3/switch", methods=["GET"])
def wg_api_v3_switch() -> tuple[Response | dict, int]:
    """A node reports that it moved its tunnel of a site to another pgw.

    The broker accepts the report only when that pgw has reported a fresh
    handshake of the node. Then it stores that pgw as the preferred pgw of the
    site and tells the other pgws of the site to remove the route to the node
    and keep the peer (standby).
    """
    parker_cfg = config.get_config().parker
    if not parker_cfg.enabled or not parker_cfg.sites:
        return {"error": {"message": "Multi-site Parker is not enabled"}}, 400

    site = request.args.get("site", "")
    pgw = request.args.get("pgw", "")
    try:
        pubkey = is_valid_wg_pubkey(request.args.get("pubkey", ""))
    except ValueError:
        return {"error": {"message": "Invalid public key"}}, 400
    site_pgws = config.get_config().workers.names_in_pop(site)
    if site not in parker_cfg.sites or pgw not in site_pgws:
        return {"error": {"message": "Unknown site or pgw"}}, 400

    reported = handshakes.get(pubkey)
    if (
        reported is None
        or reported[0] != pgw
        or time.monotonic() - reported[1] > _handshake_max_age()
    ):
        return {"error": {"message": "No fresh handshake on this pgw yet"}}, 409

    logger.info("Switch of %s to %s in site %s", pubkey, pgw, site)
    try:
        site_ipams[site].update_prefix(pubkey, not parker_cfg.xlat, True, [pgw])
    except Exception as error:  # noqa: BLE001
        logger.error("Could not store the switch of %s", pubkey, exc_info=error)
        return {"error": {"message": "Internal error, please try again later"}}, 500
    _unroute(pubkey, [other for other in site_pgws if other != pgw])
    return {"Message": "OK"}, 200


def _unroute(pubkey: str, workers: list[str], handshake: float | None = None) -> None:
    """Tells the workers to remove the route to a node and keep its peer."""
    data: dict[str, Any] = {"PublicKey": pubkey, "Unroute": True}
    if handshake is not None:
        data["Handshake"] = handshake
    payload = json.dumps(data).encode("utf-8")
    for worker in workers:
        mqtt.publish(
            MQTTTopics.TOPIC_PARKER_WG_PEER.format(worker=worker), payload, qos=1
        )


@mqtt.on_connect()
def handle_mqtt_connect(
    client: mqtt_client.Client, userdata: bytes, flags: Any, rc: Any
) -> None:
    """Subscribes to all broker topics.

    Runs on every (re)connect, so subscriptions are restored after a reconnect.
    """
    broker = (app.config["MQTT_BROKER_URL"], app.config["MQTT_BROKER_PORT"])
    if rc != paho.mqtt.enums.ConnackCode.CONNACK_ACCEPTED:
        logger.error(
            "MQTT connection to %s:%s failed with return code %s (%s)",
            *broker,
            rc,
            paho.mqtt.enums.ConnackCode(rc).name,
        )
        return
    logger.debug("MQTT connected to %s:%s", *broker)
    subscriptions.subscribe(mqtt, "wireguard-metrics/#")
    subscriptions.subscribe(mqtt, TOPIC_WORKER_STATUS.format(worker="+"))
    subscriptions.subscribe(mqtt, TOPIC_WORKER_WG_DATA.format(worker="+", domain="+"))
    if config.get_config().load_balancing.share_pending_assignments:
        subscriptions.subscribe(mqtt, TOPIC_BROKER_ASSIGNMENT.format(broker="+"))
    if config.get_config().parker.enabled:
        logger.debug("Parker mode is enabled, subscribing to parker topics")
        subscriptions.subscribe(
            mqtt, MQTTTopics.TOPIC_PARKER_CONNECTED_PEERS.format(worker="+")
        )
        subscriptions.subscribe(
            mqtt, MQTTTopics.TOPIC_PARKER_WORKER_STATUS.format(worker="+")
        )
        subscriptions.subscribe(
            mqtt, MQTTTopics.TOPIC_PARKER_WORKER_WG_DATA.format(worker="+")
        )
        if config.get_config().parker.sites:
            subscriptions.subscribe(
                mqtt, MQTTTopics.TOPIC_PARKER_WORKER_HANDSHAKE.format(worker="+"), qos=1
            )
        if ipam_caches:
            subscriptions.subscribe(mqtt, TOPIC_BROKER_IPAM.format(broker="+"), qos=1)


@mqtt.on_subscribe()
def handle_mqtt_subscribe(
    client: mqtt_client.Client, userdata: bytes, mid: int, granted_qos: Any
) -> None:
    """Retries subscriptions rejected by the MQTT broker."""
    subscriptions.on_subscribe(client, userdata, mid, granted_qos)


@mqtt.on_topic("wireguard-metrics/#")
def handle_mqtt_message_metrics(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Processes published metrics from workers."""
    logger.debug(
        f"MQTT message received on {message.topic}: {_decode_payload(message.payload)}"
    )
    try:
        _, domain, worker, metric = message.topic.split("/", 3)
    except ValueError:
        logger.error(f"Ignored MQTT message on unexpected topic {message.topic}")
        MQTT_MESSAGE_ERRORS.labels("metrics").inc()
        return
    if not is_valid_domain(domain):
        logger.error(f"Domain {domain} not in configured domains")
        return

    if not worker or not metric:
        logger.error("Ignored MQTT message with empty worker or metrics label")
        return

    try:
        data = int(message.payload)
    except (TypeError, ValueError):
        logger.error(
            f"Ignored invalid metric on {message.topic}: {_decode_payload(message.payload)}"
        )
        MQTT_MESSAGE_ERRORS.labels("metrics").inc()
        return

    logger.debug(f"Update worker metrics: {metric} on {worker}/{domain} = {data}")
    worker_metrics.update(worker, domain, metric, data)


@mqtt.on_topic("parker/wireguard-metrics/#")
def handle_mqtt_message_parker_metrics(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Processes published metrics from workers."""
    logger.debug(
        f"MQTT message received on {message.topic}: {message.payload.decode()}"
    )
    _, _, worker, metric = message.topic.split("/", 3)

    if not worker or not metric:
        logger.error("Ignored MQTT message with empty worker or metrics label")
        return

    try:
        data = int(message.payload)
    except (TypeError, ValueError):
        logger.error(
            "Invalid metrics payload for %s/%s: %s", worker, metric, message.payload
        )
        return

    logger.info(f"Update Parker worker metrics: {metric} on {worker} = {data}")
    parker_worker_metrics.update(worker, "parker", metric, data)


@mqtt.on_topic(MQTTTopics.TOPIC_PARKER_WORKER_HANDSHAKE.format(worker="+"))
def handle_mqtt_message_parker_handshake(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Stores which worker saw the last fresh handshake of a node.

    The node now sends its traffic through this worker. So the broker tells
    the other pgws of the same site to remove their route to the node at once.
    Without this, they keep the route until the old handshake is too old, and
    the upstream routers send a part of the return traffic to the old pgw.
    """
    _, _, worker, _ = message.topic.split("/", 3)
    try:
        data = json.loads(message.payload)
        public_key = is_valid_wg_pubkey(data["PublicKey"])
    except (ValueError, TypeError, KeyError) as error:
        logger.error("Invalid handshake event from %s: %s", worker, error)
        return
    handshake = data.get("Handshake")
    if not isinstance(handshake, (int, float)) or isinstance(handshake, bool):
        handshake = None
    # Use the receive time of this broker, not the time of the worker, so
    # that clock differences between hosts do not matter.
    now = time.monotonic()
    handshakes[public_key] = (worker, now)
    max_age = _handshake_max_age()
    for key in [k for k, (_, seen) in handshakes.items() if now - seen > max_age]:
        del handshakes[key]

    cfg = config.get_config()
    worker_cfg = cfg.workers.get(worker)
    if worker_cfg is None or worker_cfg.pop not in (cfg.parker.sites or {}):
        return
    site_handshakes[(worker_cfg.pop, public_key)] = (worker, now)
    for key in [k for k, (_, seen) in site_handshakes.items() if now - seen > max_age]:
        del site_handshakes[key]
    others = [w for w in cfg.workers.names_in_pop(worker_cfg.pop) if w != worker]
    logger.info("Unroute %s on %s, it moved to %s", public_key, others, worker)
    _unroute(public_key, others, handshake)


@mqtt.on_topic(TOPIC_BROKER_IPAM.format(broker="+"))
def handle_mqtt_message_ipam(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Adds an IPAM change of another broker to the cache."""
    sender = message.topic.split("/")[1]
    if sender == broker_id:
        return
    try:
        data = json.loads(message.payload)
        cache = ipam_caches[data["site"]]
        cache.apply(is_valid_wg_pubkey(data["pubkey"]), data["entry"])
    except Exception as error:  # noqa: BLE001
        logger.error("Ignoring invalid IPAM message from broker %s: %s", sender, error)
        MQTT_MESSAGE_ERRORS.labels("ipam").inc()


@mqtt.on_topic(MQTTTopics.TOPIC_WORKER_STATUS.format(worker="+"))
def handle_mqtt_message_status(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Processes status messages from workers."""
    _, worker, _ = message.topic.split("/", 2)

    try:
        status = int(message.payload)
    except (TypeError, ValueError):
        logger.error(
            f"Ignored invalid status on {message.topic}: {_decode_payload(message.payload)}"
        )
        MQTT_MESSAGE_ERRORS.labels("status").inc()
        return
    if status < 1:
        if worker_metrics.get(worker).is_online():
            logger.warning(f"Marking worker as offline: {worker}")
            worker_metrics.set_offline(worker)
        return
    if not worker_metrics.get(worker).is_online():
        logger.warning(f"Marking worker as online: {worker}")
        worker_metrics.set_online(worker)
    # Status 2: draining, higher values are treated as online for forward compatibility
    draining = status == STATUS_DRAINING
    if draining != worker_metrics.is_draining(worker):
        logger.warning(
            f"Worker {worker} is {'draining' if draining else 'no longer draining'}"
        )
        worker_metrics.set_draining(worker, draining)


@mqtt.on_topic(MQTTTopics.TOPIC_PARKER_WORKER_STATUS.format(worker="+"))
def handle_mqtt_message_parker_status(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Processes status messages from workers."""
    _, _, worker, _ = message.topic.split("/", 3)

    try:
        status = int(message.payload)
    except (TypeError, ValueError):
        logger.error(
            "Invalid worker status payload for %s: %s", worker, message.payload
        )
        return
    if status < 1:
        if parker_worker_metrics.get(worker).is_online():
            logger.warning(f"Marking Parker worker as offline: {worker}")
            parker_worker_metrics.set_offline(worker)
        return
    if not parker_worker_metrics.get(worker).is_online():
        logger.warning(f"Marking Parker worker as online: {worker}")
        parker_worker_metrics.set_online(worker)
    draining = status == STATUS_DRAINING
    if draining != parker_worker_metrics.is_draining(worker):
        logger.warning(
            f"Parker worker {worker} is {'draining' if draining else 'no longer draining'}"
        )
        parker_worker_metrics.set_draining(worker, draining)


@mqtt.on_topic(MQTTTopics.TOPIC_WORKER_WG_DATA.format(worker="+", domain="+"))
def handle_mqtt_message_data(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Processes data messages from workers.

    Stores them in a local dict"""
    _, worker, domain, _ = message.topic.split("/", 3)
    if not is_valid_domain(domain):
        logger.debug(f"Domain {domain} not in configured domains.")
        return

    try:
        data = json.loads(message.payload)
    except ValueError:
        logger.error(
            "Invalid worker data received for %s/%s: %s",
            worker,
            domain,
            _decode_payload(message.payload),
        )
        MQTT_MESSAGE_ERRORS.labels("data").inc()
        return
    if not isinstance(data, dict):
        logger.error("Invalid worker data received for %s/%s: %s", worker, domain, data)
        MQTT_MESSAGE_ERRORS.labels("data").inc()
        return

    logger.info("Worker data received for %s/%s: %s", worker, domain, data)
    worker_data[(worker, domain)] = data


@mqtt.on_topic(TOPIC_BROKER_ASSIGNMENT.format(broker="+"))
def handle_mqtt_message_assignment(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Counts assignments of other broker instances as pending."""
    if not config.get_config().load_balancing.share_pending_assignments:
        return
    sender = message.topic.split("/")[1]
    if sender == broker_id:
        return
    try:
        data = json.loads(message.payload)
        worker, domain, key = str(data["worker"]), str(data["domain"]), data["key"]
        is_valid_wg_pubkey(key)
    except Exception as e:  # noqa: BLE001
        logger.error(
            f"Ignoring invalid assignment message from broker {sender}: "
            f"{_decode_payload(message.payload)} ({e})"
        )
        MQTT_MESSAGE_ERRORS.labels("assignment").inc()
        return
    logger.debug(f"Broker {sender} assigned {key} to {worker}/{domain}")
    worker_metrics.record_assignment(worker, domain, key)
    assignment_history.record(key, domain, worker)
    SHARED_ASSIGNMENTS.inc()


@mqtt.on_topic(MQTTTopics.TOPIC_PARKER_WORKER_WG_DATA.format(worker="+", domain="+"))
def handle_mqtt_message_parker_data(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Processes data messages from workers.

    Stores them in a local dict"""
    _, _, worker, _ = message.topic.split("/", 3)

    data = json.loads(message.payload)
    if not isinstance(data, dict):
        logger.error("Invalid worker data received for %s: %s", worker, data)
        return

    logger.info("Worker data received for %s: %s", worker, data)
    parker_worker_data[worker] = data


@mqtt.on_message()
def handle_mqtt_message(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Prints message contents."""
    logger.debug(
        f"MQTT message received on {message.topic}: {_decode_payload(message.payload)}"
    )


def _decode_payload(payload: bytes) -> str:
    """Decodes an MQTT payload for logging, never raising on invalid data."""
    return payload.decode("utf-8", errors="replace")


def publish_rebalance_hints() -> None:
    """Publishes rebalancing hints to workers which have more peers than their weighted target."""
    cfg = config.get_config()
    rebalancing = cfg.load_balancing.rebalancing
    hints = worker_metrics.compute_rebalance_hints(
        domains=cfg.domains,
        tolerance=rebalancing.tolerance,
        min_excess=rebalancing.min_excess_peers,
        stable_time=rebalancing.stable_time,
        drain_rate=rebalancing.drain_rate,
    )
    for worker, hint in hints.items():
        logger.info(f"Asking worker {worker} to shed peers: {hint}")
        kind = "rebalance" if hint.max_evictions_ratio is None else "drain"
        REBALANCE_HINTS.labels(worker, kind).inc()
        REBALANCE_HINT_EXCESS.labels(worker, kind).inc(hint.excess)
        mqtt.publish(
            TOPIC_WORKER_REBALANCE.format(worker=worker),
            json.dumps(hint.to_dict()),
            qos=0,
            retain=False,
        )


def rebalance_loop() -> None:
    """Periodically publishes rebalancing hints."""
    interval = config.get_config().load_balancing.rebalancing.interval
    while True:
        time.sleep(interval)
        try:
            publish_rebalance_hints()
        except Exception as e:  # noqa: BLE001
            # Don't crash the thread when an exception is encountered
            logger.error("Exception during rebalancing task: %s", e, exc_info=True)


def join_host_port(host: str, port: str) -> str:
    """Concatenate a port string with a host string using a colon.
    The host may be either a hostname, IPv4 or IPv6 address.
    An IPv6 address as host will be automatically encapsulated in square brackets.

    Returns:
        The joined host:port string
    """
    if host.find(":") >= 0:
        return "[" + host + "]:" + port
    return host + ":" + port


# Connect only now: the paho network loop starts here, and a CONNACK received before
# handle_mqtt_connect was registered would leave the broker without subscriptions.
mqtt.init_app(app)


if __name__ == "__main__":
    listen_host = None
    listen_port = None

    listen_config = config.get_config().broker_listen
    if listen_config is not None:
        listen_host = listen_config.host
        listen_port = listen_config.port

    if config.get_config().load_balancing.rebalancing.enabled:
        logger.info("Active rebalancing is enabled")
        threading.Thread(target=rebalance_loop, daemon=True).start()

    for cache in ipam_caches.values():
        threading.Thread(
            target=cache.run, args=(threading.Event(),), daemon=True
        ).start()
    if ipam_caches and config.get_config().parker.ipam_cache.reclaim_days:
        threading.Thread(
            target=reclaim_loop, args=(threading.Event(),), daemon=True
        ).start()

    serve(app, host=listen_host, port=listen_port)
