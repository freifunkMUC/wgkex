"""wgkex broker"""

import dataclasses
import json
import re
import socket
import threading
import time
import uuid
from typing import Any

import paho.mqtt.client as mqtt_client
from flask import Flask, Response, abort, render_template, request
from flask.app import Flask as Flask_app
from flask_mqtt import Mqtt
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from waitress import serve

from wgkex.broker import observability
from wgkex.broker.assignments import Assignment, AssignmentHistory
from wgkex.broker.metrics import WorkerMetricsCollection
from wgkex.broker.observability import (
    ASSIGNMENTS,
    KEY_EXCHANGE_REQUESTS,
    MQTT_MESSAGE_ERRORS,
    REBALANCE_HINT_EXCESS,
    REBALANCE_HINTS,
    SHARED_ASSIGNMENTS,
)
from wgkex.broker.ratelimit import RateLimiter, client_address
from wgkex.common import logger
from wgkex.common.keylist import KeyList
from wgkex.common.mqtt import (
    STATUS_DRAINING,
    TOPIC_BROKER_ASSIGNMENT,
    TOPIC_WG_KEY,
    TOPIC_WORKER_REBALANCE,
    TOPIC_WORKER_REMOVE_PEER,
    TOPIC_WORKER_STATUS,
    TOPIC_WORKER_WG_DATA,
    WG_KEY_ALL_WORKERS,
)
from wgkex.common.utils import is_valid_domain
from wgkex.config import config

WG_PUBKEY_PATTERN = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$")
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


app = _fetch_app_config()
mqtt = Mqtt(app)
_load_balancing_cfg = config.get_config().load_balancing
worker_metrics = WorkerMetricsCollection(
    pending_ttl=_load_balancing_cfg.pending_assignment_ttl,
    pending_settle_time=_load_balancing_cfg.pending_assignment_settle_time,
    metrics_max_age=_load_balancing_cfg.metrics_max_age,
)
worker_data: dict[tuple[str, str], dict] = {}
assignment_history = AssignmentHistory(
    ttl=_load_balancing_cfg.previous_assignment_ttl,
    max_entries=_load_balancing_cfg.previous_assignment_max_entries,
)
observability.register_worker_metrics(worker_metrics)
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


rate_limiter = _create_rate_limiter()
blocklist = _create_blocklist()


def _blocked_response(api: str, key: str) -> tuple[dict, int]:
    logger.warning(f"Rejected blocked key {key} on the {api} API")
    KEY_EXCHANGE_REQUESTS.labels(api, "blocked").inc()
    return {"error": {"message": "key is blocked"}}, 403


def _is_rate_limited(api: str, key: str) -> bool:
    """Takes a rate limit token for the current request, returns True if the request is limited."""
    address = client_address(
        request.remote_addr,
        request.headers.get("X-Forwarded-For"),
        config.get_config().rate_limit.trusted_proxy_hops,
    )
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
    logger.info(f"wg_api_v1_key_exchange: Domain: {domain}, Key:{key}")
    if key in blocklist:
        return _blocked_response("v1", key)
    if _is_rate_limited("v1", key):
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
    logger.info(
        f"wg_api_v2_key_exchange: Domain: {domain}, Key:{key}, Location: {data.location}"
    )
    if key in blocklist:
        return _blocked_response("v2", key)
    limited = _is_rate_limited("v2", key)

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


@mqtt.on_connect()
def handle_mqtt_connect(
    client: mqtt_client.Client, userdata: bytes, flags: Any, rc: Any
) -> None:
    """Prints status of connect message."""
    # TODO(ruairi): Clarify current usage of this function.
    logger.debug(
        "MQTT connected to {}:{}".format(
            app.config["MQTT_BROKER_URL"], app.config["MQTT_BROKER_PORT"]
        )
    )
    mqtt.subscribe("wireguard-metrics/#")
    mqtt.subscribe(TOPIC_WORKER_STATUS.format(worker="+"))
    mqtt.subscribe(TOPIC_WORKER_WG_DATA.format(worker="+", domain="+"))
    if config.get_config().load_balancing.share_pending_assignments:
        mqtt.subscribe(TOPIC_BROKER_ASSIGNMENT.format(broker="+"))


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
    except ValueError:
        logger.error(
            f"Ignored invalid metric on {message.topic}: {_decode_payload(message.payload)}"
        )
        MQTT_MESSAGE_ERRORS.labels("metrics").inc()
        return

    logger.debug(f"Update worker metrics: {metric} on {worker}/{domain} = {data}")
    worker_metrics.update(worker, domain, metric, data)


@mqtt.on_topic(TOPIC_WORKER_STATUS.format(worker="+"))
def handle_mqtt_message_status(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Processes status messages from workers."""
    _, worker, _ = message.topic.split("/", 2)

    try:
        status = int(message.payload)
    except ValueError:
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


@mqtt.on_topic(TOPIC_WORKER_WG_DATA.format(worker="+", domain="+"))
def handle_mqtt_message_data(
    client: mqtt_client.Client, userdata: bytes, message: mqtt_client.MQTTMessage
) -> None:
    """Processes data messages from workers.

    Stores them in a local dict"""
    _, worker, domain, _ = message.topic.split("/", 3)
    if not is_valid_domain(domain):
        logger.error(f"Domain {domain} not in configured domains.")
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


def is_valid_wg_pubkey(pubkey: str) -> str:
    """Verifies if key is a valid WireGuard public key or not.

    Arguments:
        pubkey: The key to verify.

    Raises:
        ValueError: If the Wireguard Key is invalid.

    Returns:
        The public key.
    """
    # TODO(ruairi): Refactor to return bool.
    if WG_PUBKEY_PATTERN.match(pubkey) is None:
        raise ValueError(f"Not a valid Wireguard public key: {pubkey}.")
    return pubkey


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

    serve(app, host=listen_host, port=listen_port)
