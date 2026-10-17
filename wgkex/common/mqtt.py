"""Common MQTT constants like topic string templates"""

TOPIC_WORKER_WG_DATA = "wireguard-worker/{worker}/{domain}/data"
TOPIC_WORKER_STATUS = "wireguard-worker/{worker}/status"
# Retained worker status payloads. Brokers from before drain mode treat any status >= 1 as online.
STATUS_OFFLINE = 0
STATUS_ONLINE = 1
STATUS_DRAINING = 2
CONNECTED_PEERS_METRIC = "connected_peers"
TOPIC_CONNECTED_PEERS = "wireguard-metrics/{domain}/{worker}/" + CONNECTED_PEERS_METRIC
# Broker -> worker rebalancing hints (JSON: {"excess": int, "domains": [str, ...]}), not retained.
# Hints for draining workers additionally contain "max_evictions_ratio": float.
# Only acted upon by workers which have rebalancing enabled; ignored by older workers.
TOPIC_WORKER_REBALANCE = "wireguard-worker/{worker}/rebalance"
# Broker -> worker: keys exchanged via the v1 API are published to "all", keys exchanged via the v2
# API to the chosen worker only (unless targeted publishing is disabled). Payload: the public key.
# Old workers subscribe to wireguard/{domain}/+ and add every key, new workers only act on "all"
# and their own hostname.
TOPIC_WG_KEY = "wireguard/{domain}/{worker}"
WG_KEY_ALL_WORKERS = "all"
# Broker -> worker: remove the peer with this public key (payload), because it was assigned to
# another worker. Not retained, ignored by older workers.
TOPIC_WORKER_REMOVE_PEER = "wireguard-worker/{worker}/{domain}/remove"
# Broker -> broker: a key was assigned to a worker by the v2 API (JSON: {"worker": str,
# "domain": str, "key": str}), not retained. Only used if share_pending_assignments is enabled, so
# that multiple broker instances count each other's pending assignments. Ignored by older brokers.
TOPIC_BROKER_ASSIGNMENT = "wireguard-broker/{broker}/assignment"

# Parker (/api/v3) topics. Parker workers serve a single wg-nodes interface without domains.
TOPIC_PARKER_WORKER_WG_DATA = "parker/wireguard-worker/{worker}/data"
TOPIC_PARKER_WORKER_STATUS = "parker/" + TOPIC_WORKER_STATUS
TOPIC_PARKER_CONNECTED_PEERS = (
    "parker/wireguard-metrics/{worker}/" + CONNECTED_PEERS_METRIC
)
# Broker -> worker: Parker peer updates (JSON), addressed to a worker hostname or "all".
TOPIC_PARKER_WG_PEER = "parker/wireguard/{worker}"


class MQTTTopics:
    """Namespace for the topic templates above."""

    TOPIC_WORKER_WG_DATA = TOPIC_WORKER_WG_DATA
    TOPIC_WORKER_STATUS = TOPIC_WORKER_STATUS
    CONNECTED_PEERS_METRIC = CONNECTED_PEERS_METRIC
    TOPIC_CONNECTED_PEERS = TOPIC_CONNECTED_PEERS

    TOPIC_PARKER_WORKER_WG_DATA = TOPIC_PARKER_WORKER_WG_DATA
    TOPIC_PARKER_WORKER_STATUS = TOPIC_PARKER_WORKER_STATUS
    TOPIC_PARKER_CONNECTED_PEERS = TOPIC_PARKER_CONNECTED_PEERS
    TOPIC_PARKER_WG_PEER = TOPIC_PARKER_WG_PEER
