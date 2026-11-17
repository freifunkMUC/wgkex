"""Resubscribes MQTT topics whose subscription was rejected by the MQTT broker."""

import threading
from typing import Any

from wgkex.common import logger

# SUBACK granted QoS value for a rejected subscription (MQTT 3.1.1 failure, MQTT 5 reason codes >= 0x80).
SUBACK_FAILURE = 0x80
RETRY_DELAY = 5.0


class SubscriptionGuard:
    """Subscribes to topics and retries those that the MQTT broker answers with a SUBACK failure.

    A MQTT broker which just restarted may accept the connection before it loaded its authorization
    rules and then rejects every SUBSCRIBE. The client stays connected and would never resubscribe.
    """

    def __init__(self, retry_delay: float = RETRY_DELAY) -> None:
        self._retry_delay = retry_delay
        self._lock = threading.Lock()
        self._pending: dict[int, tuple[Any, str, int]] = {}

    def subscribe(self, client: Any, topic: str, qos: int = 0) -> None:
        """Subscribes client (paho client or flask_mqtt.Mqtt) to topic and tracks the SUBACK."""
        # Hold the lock so on_subscribe cannot see the SUBACK before the mid is recorded.
        with self._lock:
            result, mid = client.subscribe(topic, qos=qos)
            if result == 0:
                self._pending[mid] = (client, topic, qos)

    def on_subscribe_v2(
        self,
        client: Any,
        userdata: Any,
        mid: int,
        reason_code_list: Any,
        properties: Any = None,
    ) -> None:
        """Handler for the paho CallbackAPIVersion.VERSION2 on_subscribe callback."""
        self.on_subscribe(client, userdata, mid, reason_code_list)

    def on_subscribe(
        self, client: Any, userdata: Any, mid: int, granted_qos: Any
    ) -> None:
        """Handler for the on_subscribe callback."""
        with self._lock:
            entry = self._pending.pop(mid, None)
        # Flask-MQTT passes ints, paho VERSION2 passes ReasonCode objects (int value in .value).
        if entry is None or not any(
            getattr(q, "value", q) >= SUBACK_FAILURE for q in granted_qos
        ):
            return
        _, topic, qos = entry
        logger.error(
            f"MQTT subscription to {topic} was rejected, retrying in {self._retry_delay}s"
        )
        timer = threading.Timer(
            self._retry_delay, self.subscribe, args=(entry[0], topic, qos)
        )
        timer.daemon = True
        timer.start()
