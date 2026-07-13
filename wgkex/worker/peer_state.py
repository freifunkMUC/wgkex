"""Bounded worker-local state used to coordinate peer mutations."""

import threading
import time
from collections import OrderedDict
from collections.abc import Iterable
from contextlib import contextmanager
from typing import Callable, Iterator


class PeerMutationCoordinator:
    """Coordinates per-peer mutations and tracks recent provisioning."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        lock_stripes: int = 256,
        max_provisioned_peers: int = 65536,
    ) -> None:
        if lock_stripes <= 0:
            raise ValueError("lock_stripes must be positive")
        if max_provisioned_peers <= 0:
            raise ValueError("max_provisioned_peers must be positive")

        self._clock = clock
        self._locks = tuple(threading.RLock() for _ in range(lock_stripes))
        self._max_provisioned_peers = max_provisioned_peers
        self._provisioned_at: OrderedDict[str, float] = OrderedDict()
        self._pending_parker_routes: OrderedDict[str, None] = OrderedDict()
        self._state_lock = threading.Lock()
        self._started_at = clock()

    @contextmanager
    def peer_lock(
        self,
        public_key: str,
        related_resources: str | Iterable[str] | None = None,
    ) -> Iterator[None]:
        """Lock a peer and optional related resources in stable order."""
        resources = [f"peer:{public_key}"]
        if isinstance(related_resources, str):
            related_resources = [related_resources]
        if related_resources is not None:
            resources.extend(f"resource:{resource}" for resource in related_resources)
        lock_indexes = sorted(
            {hash(resource) % len(self._locks) for resource in resources}
        )
        locks = [self._locks[index] for index in lock_indexes]
        for lock in locks:
            lock.acquire()
        try:
            yield
        finally:
            for lock in reversed(locks):
                lock.release()

    def _prune_expired(self, now: float) -> None:
        expired = [
            public_key
            for public_key, expires_at in self._provisioned_at.items()
            if expires_at <= now
        ]
        for public_key in expired:
            del self._provisioned_at[public_key]

    def record_provisioned(
        self, public_key: str, retention_seconds: float = 600
    ) -> None:
        """Record an accepted queue update before applying it to netlink."""
        now = self._clock()
        with self._state_lock:
            self._prune_expired(now)
            if public_key in self._provisioned_at:
                del self._provisioned_at[public_key]
            elif len(self._provisioned_at) >= self._max_provisioned_peers:
                raise RuntimeError("recent peer provisioning tracker is at capacity")
            self._provisioned_at[public_key] = now + retention_seconds

    def recently_provisioned(self, public_key: str, grace_seconds: float) -> bool:
        """Return whether a peer has a queue update inside the grace period."""
        now = self._clock()
        with self._state_lock:
            expires_at = self._provisioned_at.get(public_key)
            if expires_at is None:
                return False
            if now < expires_at:
                return True
            del self._provisioned_at[public_key]
            return False

    def defer_never_handshaked(self, public_key: str, grace_seconds: float) -> bool:
        """Protect never-handshaked peers after provisioning or worker restart."""
        now = self._clock()
        with self._state_lock:
            expires_at = self._provisioned_at.get(public_key)
            if expires_at is not None and now < expires_at:
                return True
            self._provisioned_at.pop(public_key, None)
            return now - self._started_at < grace_seconds

    def forget(self, public_key: str) -> None:
        """Forget state for a peer after it has been removed."""
        with self._state_lock:
            self._provisioned_at.pop(public_key, None)

    def record_pending_parker_route(self, prefix: str) -> None:
        """Remember an old Parker route that still needs deletion."""
        with self._state_lock:
            if prefix in self._pending_parker_routes:
                self._pending_parker_routes.move_to_end(prefix)
                return
            if len(self._pending_parker_routes) >= self._max_provisioned_peers:
                raise RuntimeError("pending Parker route tracker is at capacity")
            self._pending_parker_routes[prefix] = None

    def pending_parker_routes(self) -> tuple[str, ...]:
        """Return a snapshot of old Parker routes awaiting deletion."""
        with self._state_lock:
            return tuple(self._pending_parker_routes)

    def forget_pending_parker_route(self, prefix: str) -> None:
        """Forget an old Parker route after deletion or reassignment."""
        with self._state_lock:
            self._pending_parker_routes.pop(prefix, None)


peer_mutations = PeerMutationCoordinator()
