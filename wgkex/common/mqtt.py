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
