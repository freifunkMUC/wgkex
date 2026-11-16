"""Token bucket rate limiting of key exchange requests per client address and globally."""

import ipaddress
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence


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


TrustedProxies = int | Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network]


def _parse_address(
    value: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        addr = ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        return addr.ipv4_mapped
    return addr


def client_address(
    remote_addr: str | None, forwarded_for: str | None, trusted_proxies: TrustedProxies
) -> str | None:
    """Determines the client address, only trusting X-Forwarded-For from trusted reverse proxies.

    Each proxy appends the address it received the request from to X-Forwarded-For, so the header
    is evaluated from the right and anything left of the trusted part may be spoofed by the client.

    trusted_proxies is either
    - a number n of proxies which every request passes: the client address is the n-th entry from
      the right (like werkzeug's ProxyFix(x_for=n)). If the header has fewer entries, the TCP peer
      address is used.
    - a list of networks: if the TCP peer is in one of them, the header is walked from the right,
      skipping addresses in trusted networks, and the first other address is the client. The header
      is ignored for requests from untrusted peers.
    """
    if not forwarded_for or not trusted_proxies:
        return remote_addr
    hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
    if isinstance(trusted_proxies, int):
        if trusted_proxies < 0 or len(hops) < trusted_proxies:
            return remote_addr
        return hops[-trusted_proxies]

    def trusted(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return any(
            addr.version == net.version and addr in net for net in trusted_proxies
        )

    peer = _parse_address(remote_addr or "")
    if peer is None or not trusted(peer):
        return remote_addr
    client = remote_addr
    for hop in reversed(hops):
        addr = _parse_address(hop)
        if addr is None:
            # Garbage in the header, stop at the last proxy that could be verified
            return client
        client = str(addr)
        if not trusted(addr):
            return client
    return client
