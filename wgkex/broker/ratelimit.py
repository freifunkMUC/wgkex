"""Token bucket rate limiting of key exchange requests per client address and globally."""

import ipaddress
import threading
import time
from collections import OrderedDict
from collections.abc import Callable


class TokenBucket:
    """A token bucket holding up to capacity tokens, refilled at capacity per period seconds."""

    def __init__(self, capacity: int, period: float, now: float) -> None:
        self.capacity = capacity
        self.rate = capacity / period
        self.tokens = float(capacity)
        self.updated_at = now

    def refill(self, now: float) -> None:
        self.tokens = min(
            self.capacity, self.tokens + (now - self.updated_at) * self.rate
        )
        self.updated_at = now

    def take(self, now: float) -> bool:
        self.refill(now)
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True

    def give_back(self) -> None:
        self.tokens = min(self.capacity, self.tokens + 1)

    def is_full(self, now: float) -> bool:
        self.refill(now)
        return self.tokens >= self.capacity


class RateLimiter:
    """Thread-safe rate limiter with a token bucket per client and a global one.

    A request is allowed if both the client's and the global bucket have a token. Clients whose
    bucket is full again are forgotten; at most max_clients buckets are kept (least recently used
    are dropped first, which only makes the limiter more permissive).

    Arguments:
        per_client_requests: Burst size and requests per per_client_period of a client, 0 disables.
        global_requests: Burst size and requests per global_period of all clients, 0 disables.
        ipv6_prefix_length: IPv6 addresses are limited per prefix of this length.
    """

    def __init__(
        self,
        per_client_requests: int,
        per_client_period: float,
        global_requests: int,
        global_period: float,
        ipv6_prefix_length: int = 64,
        max_clients: int = 100000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._per_client_requests = per_client_requests
        self._per_client_period = per_client_period
        self._ipv6_prefix_length = ipv6_prefix_length
        self._max_clients = max_clients
        self._clock = clock
        self._lock = threading.Lock()
        self._clients: OrderedDict[str, TokenBucket] = OrderedDict()
        self._global = (
            TokenBucket(global_requests, global_period, clock())
            if global_requests > 0
            else None
        )
        self._last_cleanup = clock()

    def client_id(self, address: str | None) -> str:
        """Returns the address, or its prefix for IPv6 addresses."""
        if not address:
            return ""
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return address
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.ipv4_mapped is not None:
                return str(ip.ipv4_mapped)
            return str(
                ipaddress.ip_network(f"{ip}/{self._ipv6_prefix_length}", strict=False)
            )
        return str(ip)

    def allow(self, address: str | None) -> tuple[bool, str]:
        """Takes a token for a request of the given client.

        Returns:
            Whether the request is allowed, and the limit hit ("client" or "global") otherwise.
        """
        now = self._clock()
        with self._lock:
            self._cleanup(now)
            bucket = None
            if self._per_client_requests > 0:
                key = self.client_id(address)
                bucket = self._clients.pop(key, None) or TokenBucket(
                    self._per_client_requests, self._per_client_period, now
                )
                self._clients[key] = bucket
                while len(self._clients) > self._max_clients:
                    self._clients.popitem(last=False)
                if not bucket.take(now):
                    return False, "client"
            if self._global is not None and not self._global.take(now):
                if bucket is not None:
                    bucket.give_back()
                return False, "global"
            return True, ""

    def tracked_clients(self) -> int:
        with self._lock:
            return len(self._clients)

    def _cleanup(self, now: float) -> None:
        if now - self._last_cleanup < self._per_client_period:
            return
        self._last_cleanup = now
        for key in [k for k, b in self._clients.items() if b.is_full(now)]:
            del self._clients[key]


def client_address(
    remote_addr: str | None, forwarded_for: str | None, trusted_proxy_hops: int
) -> str | None:
    """Determines the client address, taking trusted reverse proxies into account.

    With n trusted proxies, the client address is the n-th entry from the right in the
    X-Forwarded-For header (each proxy appends the address it received the request from).
    """
    if trusted_proxy_hops <= 0 or not forwarded_for:
        return remote_addr
    hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
    if len(hops) < trusted_proxy_hops:
        return hops[0] if hops else remote_addr
    return hops[-trusted_proxy_hops]
