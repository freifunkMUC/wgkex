"""Common MQTT constants like topic string templates"""

# TODO only use parker prefix when parker is enabled
TOPIC_WORKER_WG_DATA = "parker/wireguard-worker/{worker}/{domain}/data"
TOPIC_WORKER_STATUS = "parker/wireguard-worker/{worker}/status"
CONNECTED_PEERS_METRIC = "connected_peers"
TOPIC_CONNECTED_PEERS = (
    "parker/wireguard-metrics/{domain}/{worker}/" + CONNECTED_PEERS_METRIC
)
