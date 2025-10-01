"""Common MQTT constants like topic string templates"""


class MQTTTopics:
    TOPIC_WORKER_WG_DATA = "wireguard-worker/{worker}/{domain}/data"
    TOPIC_WORKER_STATUS = "wireguard-worker/{worker}/status"
    CONNECTED_PEERS_METRIC = "connected_peers"
    TOPIC_CONNECTED_PEERS = (
        "wireguard-metrics/{domain}/{worker}/" + CONNECTED_PEERS_METRIC
    )

    TOPIC_PARKER_WORKER_WG_DATA = "parker/wireguard-worker/{worker}/data"
    TOPIC_PARKER_WORKER_STATUS = "parker/" + TOPIC_WORKER_STATUS
    TOPIC_PARKER_CONNECTED_PEERS = (
        "parker/wireguard-metrics/{worker}/" + CONNECTED_PEERS_METRIC
    )
