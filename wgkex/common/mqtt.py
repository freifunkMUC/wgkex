"""Common MQTT constants like topic string templates"""

TOPIC_WORKER_WG_DATA = "wireguard-worker/{worker}/{domain}/data"
TOPIC_WORKER_STATUS = "wireguard-worker/{worker}/status"
CONNECTED_PEERS_METRIC = "connected_peers"
TOPIC_CONNECTED_PEERS = "wireguard-metrics/{domain}/{worker}/" + CONNECTED_PEERS_METRIC
# Broker -> worker rebalancing hints (JSON: {"excess": int, "domains": [str, ...]}), not retained.
# Only acted upon by workers which have rebalancing enabled; ignored by older workers.
TOPIC_WORKER_REBALANCE = "wireguard-worker/{worker}/rebalance"
