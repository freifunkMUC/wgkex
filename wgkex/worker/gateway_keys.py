"""Public keys of the wgkex workers (gateways), learned from their retained MQTT worker data.

Gateways connect to each other with static WireGuard peers (e.g. from systemd-networkd) on the same wg-{domain}
interfaces that wgkex manages. wgkex must never remove these peers, also not while the other gateway is offline.
Keys are therefore never forgotten while the worker runs. After a restart, the retained worker data on the MQTT
broker also contains the keys of offline workers.
"""

import json
import threading

from wgkex.common import logger


class GatewayKeys:
    """Thread-safe set of the WireGuard public keys of all workers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: set[str] = set()
        self._synced = threading.Event()

    def add(self, public_key: str) -> bool:
        """Remembers a key, returns True if it was not known before."""
        with self._lock:
            if public_key in self._keys:
                return False
            self._keys.add(public_key)
            return True

    def handle_worker_data(
        self, worker: str, payload: bytes | str, own_worker: str
    ) -> None:
        """Learns the public key from a worker data message.

        Our own data message arrives after the retained messages of all other workers, as it is published after the
        subscription. Receiving it marks the key set as complete.
        """
        try:
            public_key = json.loads(payload)["PublicKey"]
        except (ValueError, TypeError, KeyError) as e:
            if payload:
                logger.warning("Ignoring invalid worker data of %s: %s", worker, e)
            return
        if not isinstance(public_key, str) or not public_key:
            logger.warning("Ignoring worker data of %s without public key", worker)
            return
        if self.add(public_key):
            logger.info("Protecting public key %s of worker %s", public_key, worker)
        if worker == own_worker and not self._synced.is_set():
            logger.info("Received the worker data of all gateways")
            self._synced.set()

    def __contains__(self, public_key: object) -> bool:
        with self._lock:
            return public_key in self._keys

    @property
    def synced(self) -> bool:
        """Whether the keys of all workers were received."""
        return self._synced.is_set()

    def reset(self) -> None:
        """Forgets all keys, for tests."""
        with self._lock:
            self._keys.clear()
        self._synced.clear()


gateway_keys = GatewayKeys()


def is_gateway_key(public_key: str) -> bool:
    return public_key in gateway_keys
