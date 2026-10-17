"""Bounded, expiring record of which worker a public key was last assigned to."""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Assignment:
    """Where a key was published to.

    Attributes:
        domain: The domain of the key exchange.
        worker: The worker the key was published to, or "all" if it was published to every worker.
        assigned_at: Time of the assignment.
    """

    domain: str
    worker: str
    assigned_at: float


class AssignmentHistory:
    """Thread-safe LRU map of public key -> last assignment, with a TTL and a size limit.

    Arguments:
        ttl: Seconds an assignment is remembered.
        max_entries: Maximum number of remembered assignments, the oldest are dropped first.
    """

    def __init__(
        self,
        ttl: float,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._max_entries = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, Assignment] = OrderedDict()

    def record(self, public_key: str, domain: str, worker: str) -> Assignment | None:
        """Records a new assignment and returns the previous, unexpired one (if any)."""
        now = self._clock()
        with self._lock:
            self._expire(now)
            previous = self._entries.pop(public_key, None)
            if self._max_entries > 0:
                self._entries[public_key] = Assignment(domain, worker, now)
                while len(self._entries) > self._max_entries:
                    self._entries.popitem(last=False)
            return previous

    def get(self, public_key: str) -> Assignment | None:
        with self._lock:
            self._expire(self._clock())
            return self._entries.get(public_key)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def _expire(self, now: float) -> None:
        # Entries are ordered by assignment time, since record() always appends
        while self._entries:
            oldest = next(iter(self._entries.values()))
            if now - oldest.assigned_at <= self._ttl:
                break
            self._entries.popitem(last=False)
