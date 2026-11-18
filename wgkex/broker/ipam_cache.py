"""A copy of the NetBox IPAM in the memory of each broker (RIB).

The broker answers known nodes from memory. It reads NetBox only for unknown
nodes and in a background sync. When NetBox is down, the broker uses a local
snapshot and a block of free prefixes that it reserved before. It writes all
changes to NetBox from a queue.
"""

import dataclasses
import json
import os
import threading
import time
from collections.abc import Callable
from ipaddress import IPv4Network, IPv6Network
from typing import Any

from wgkex.broker.ipam import ParkerIPAM
from wgkex.broker.ipam_netbox import NetboxIPAM
from wgkex.common import logger

# Rewrite last_allocated_on of a known node at most once per day.
_REFRESH_AGE = 86400
# After a NetBox error, do not try NetBox again for this time (seconds).
_DOWN_TIME = 15


@dataclasses.dataclass(frozen=True)
class Entry:
    """The IPv6 prefix of a node in one site.

    Attributes:
        prefix: The prefix of the node.
        concentrators: The last selected concentrators (preferred pgw).
        primary: Whether this site is the primary site of the node.
        written: The time (UNIX seconds) of the last write to NetBox.
    """

    prefix: str
    concentrators: tuple[str, ...] = ()
    primary: bool = False
    written: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Entry":
        return cls(
            prefix=str(IPv6Network(data["prefix"])),
            concentrators=tuple(str(name) for name in data.get("concentrators", ())),
            primary=data.get("primary") is True,
            written=float(data.get("written", 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "prefix": self.prefix,
            "concentrators": list(self.concentrators),
            "primary": self.primary,
            "written": self.written,
        }


class IPAMCache(ParkerIPAM):
    """The IPAM of one site, served from memory and stored in NetBox."""

    def __init__(
        self,
        name: str,
        backend: Callable[[], NetboxIPAM],
        broker_id: str,
        notify: Callable[[str, str, dict[str, Any]], None] | None = None,
        snapshot_file: str | None = None,
        reserve: int = 32,
        prefix_length: int = 63,
        sync_interval: float = 60,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        """
        Attributes:
            name: The site name. It is also the name in the notify messages.
            backend: Creates the NetBox backend. Called again after a failure.
            broker_id: The stable name of this broker. Marks the reserved block.
            notify: Sends a new or changed entry to the other brokers. It
                returns when the message is delivered or cannot be delivered.
            snapshot_file: The file for the local snapshot. None: no snapshot.
            reserve: The size of the reserved block. 0: no block.
        """
        self.name = name
        self.broker_id = broker_id
        self.notify = notify
        self.snapshot_file = snapshot_file
        self.reserve_size = reserve
        self.prefix_length = prefix_length
        self.sync_interval = sync_interval
        # Keep local and notified changes this long when NetBox does not have them yet.
        self.grace = 5 * sync_interval
        self.clock = clock
        self.wall_clock = wall_clock
        self._backend_factory = backend
        self._backend: NetboxIPAM | None = None
        self._down_until = float("-inf")
        self._lock = threading.RLock()
        self._entries: dict[str, Entry] = {}
        self._primary: set[str] = set()
        self._block: list[str] = []
        # Public keys with an entry that is not in NetBox yet.
        self._dirty: set[str] = set()
        # Public key -> time of the last change that did not come from NetBox.
        self._changed: dict[str, float] = {}
        self._unsaved = False
        self._load_snapshot()
        try:
            self.sync()
        except Exception as error:  # noqa: BLE001
            logger.error(
                "Could not load the IPAM of site %s from NetBox, use %d entries "
                "from the snapshot: %s",
                name,
                len(self._entries),
                error,
            )

    def _call(self, op: Callable[[NetboxIPAM], Any]) -> Any:
        """Run a NetBox operation. After an error, skip NetBox for _DOWN_TIME."""
        if self.clock() < self._down_until:
            raise ConnectionError("NetBox is marked as not available")
        try:
            if self._backend is None:
                self._backend = self._backend_factory()
            return op(self._backend)
        except Exception:
            self._down_until = self.clock() + _DOWN_TIME
            raise

    def _set(
        self, pubkey: str, entry: Entry, dirty: bool = False, changed: bool = True
    ) -> None:
        with self._lock:
            self._entries[pubkey] = entry
            if entry.primary:
                self._primary.add(pubkey)
            else:
                self._primary.discard(pubkey)
            if dirty:
                self._dirty.add(pubkey)
            if changed:
                self._changed[pubkey] = self.clock()
            self._unsaved = True

    def _notify(self, pubkey: str, entry: Entry) -> None:
        if self.notify is None:
            return
        try:
            self.notify(self.name, pubkey, entry.to_dict())
        except Exception as error:  # noqa: BLE001
            logger.warning("Could not notify the other brokers: %s", error)

    def _get(self, pubkey: str) -> Entry | None:
        """Return the entry from memory. On a miss, read NetBox."""
        with self._lock:
            entry = self._entries.get(pubkey)
        if entry is not None:
            return entry
        try:
            data = self._call(lambda nb: nb.lookup(pubkey))
        except Exception as error:  # noqa: BLE001
            logger.warning("NetBox lookup of %s failed: %s", pubkey, error)
            return None
        if data is None:
            return None
        with self._lock:
            if pubkey not in self._entries:
                self._set(pubkey, Entry.from_dict(data), changed=False)
            return self._entries[pubkey]

    def _allocate(self, pubkey: str, prefix_length: int) -> Entry | None:
        """Allocate in NetBox. When NetBox is not available, use the reserved block."""
        try:
            data = self._call(lambda nb: nb.allocate(pubkey, prefix_length))
        except Exception as error:  # noqa: BLE001
            logger.warning("NetBox allocation for %s failed: %s", pubkey, error)
            data = None
        with self._lock:
            existing = self._entries.get(pubkey)
            if existing is not None:
                return existing
            if data is not None:
                entry, dirty = Entry.from_dict(data), False
            elif self._block:
                entry, dirty = Entry(self._block.pop(0)), True
                logger.warning(
                    "NetBox is not available, give %s from the reserved block of "
                    "site %s to %s",
                    entry.prefix,
                    self.name,
                    pubkey,
                )
            else:
                logger.error("No prefix available for %s in site %s", pubkey, self.name)
                return None
            self._set(pubkey, entry, dirty=dirty)
        self._notify(pubkey, entry)
        return entry

    def get_or_allocate_prefix(
        self,
        pubkey: str,
        ipv4: bool,
        ipv6: bool,
        ipv4_prefix_length: int = 22,
        ipv6_prefix_length: int = 63,
    ) -> tuple[IPv4Network | None, IPv6Network | None, list[str]]:
        if ipv4:
            raise NotImplementedError("The IPAM cache supports only 464XLAT")
        if not ipv6:
            return None, None, []
        entry = self._get(pubkey) or self._allocate(pubkey, ipv6_prefix_length)
        if entry is None:
            return None, None, []
        return None, IPv6Network(entry.prefix), list(entry.concentrators)

    def release_prefix(self, pubkey: str) -> None:
        """Delete the prefix of a node in NetBox and in memory."""
        released = self._call(lambda nb: nb.release(pubkey))
        with self._lock:
            entry = self._entries.pop(pubkey, None)
            self._primary.discard(pubkey)
            self._dirty.discard(pubkey)
            self._changed.pop(pubkey, None)
            self._unsaved = True
        logger.info(
            "Released %s of %s in site %s",
            released or (entry.prefix if entry else None),
            pubkey,
            self.name,
        )

    def seen(self) -> dict[str, float | None]:
        """Return public key -> last_seen of all nodes. Queued changes count as seen now."""
        seen = self._call(lambda nb: nb.seen())
        now = self.wall_clock()
        with self._lock:
            for pubkey in self._dirty | self._changed.keys():
                seen[pubkey] = now
        return seen

    def mark_seen(self, pubkeys: set[str]) -> int:
        return self._call(lambda nb: nb.mark_seen(pubkeys))

    def update_prefix(
        self,
        pubkey: str,
        ipv4: bool,
        ipv6: bool,
        selected_concentrators: list[str],
        primary: bool | None = None,
    ) -> None:
        with self._lock:
            entry = self._entries.get(pubkey)
            if entry is None:
                logger.warning(
                    "No prefix of %s in site %s to update", pubkey, self.name
                )
                return
            new = dataclasses.replace(
                entry,
                concentrators=tuple(selected_concentrators),
                primary=entry.primary if primary is None else primary,
            )
            changed = new != entry
            if not changed and self.wall_clock() - entry.written < _REFRESH_AGE:
                return
            self._set(pubkey, new, dirty=True, changed=changed)
        if changed:
            self._notify(pubkey, new)

    def is_primary(self, pubkey: str) -> bool:
        entry = self._get(pubkey)
        return entry is not None and entry.primary

    def count_primary(self) -> int:
        return len(self._primary)

    def apply(self, pubkey: str, data: dict[str, Any]) -> None:
        """Add an entry that another broker sent."""
        self._set(pubkey, Entry.from_dict(data))

    def flush(self) -> None:
        """Write the queued entries to NetBox. Stop at the first NetBox error."""
        with self._lock:
            pending = [(key, self._entries[key]) for key in sorted(self._dirty)]
        for pubkey, entry in pending:
            try:
                prefix = self._call(
                    lambda nb, pubkey=pubkey, entry=entry: nb.store(
                        pubkey, entry.prefix, list(entry.concentrators), entry.primary
                    )
                )
            except ValueError as error:
                logger.error("Drop the NetBox write of %s: %s", pubkey, error)
                with self._lock:
                    if self._entries.get(pubkey) == entry:
                        self._dirty.discard(pubkey)
                continue
            except Exception as error:  # noqa: BLE001
                logger.warning(
                    "NetBox is not available, %d writes are queued: %s",
                    len(self._dirty),
                    error,
                )
                return
            stored = dataclasses.replace(
                entry, prefix=prefix, written=self.wall_clock()
            )
            with self._lock:
                if self._entries.get(pubkey) != entry:
                    # Changed during the write, write again.
                    continue
                self._dirty.discard(pubkey)
                self._set(pubkey, stored, changed=prefix != entry.prefix)
            if prefix != entry.prefix:
                # Another broker allocated for the same node at the same time.
                logger.warning(
                    "Prefix of %s changes from %s to %s", pubkey, entry.prefix, prefix
                )
                self._notify(pubkey, stored)

    def sync(self) -> None:
        """Write the queue, then load all entries and the reserved block from NetBox."""
        self.flush()
        entries, reserved = self._call(lambda nb: nb.dump(self.broker_id))
        with self._lock:
            now = self.clock()
            self._changed = {
                key: at for key, at in self._changed.items() if now - at < self.grace
            }
            fresh = {key: Entry.from_dict(data) for key, data in entries.items()}
            for key in self._dirty | self._changed.keys():
                if key in self._entries:
                    fresh[key] = self._entries[key]
            self._entries = fresh
            self._primary = {key for key, entry in fresh.items() if entry.primary}
            used = {entry.prefix for entry in fresh.values()}
            self._block = [prefix for prefix in reserved if prefix not in used]
            missing = self.reserve_size - len(self._block)
            self._unsaved = True
        if missing > self.reserve_size // 2:
            try:
                new = self._call(
                    lambda nb: nb.reserve(self.broker_id, missing, self.prefix_length)
                )
                with self._lock:
                    self._block.extend(new)
                logger.info("Reserved %d prefixes in site %s", len(new), self.name)
            except Exception as error:  # noqa: BLE001
                logger.warning("Could not reserve prefixes: %s", error)
        self.save()

    def save(self) -> None:
        """Write the snapshot file if there are changes."""
        if self.snapshot_file is None:
            return
        with self._lock:
            if not self._unsaved:
                return
            self._unsaved = False
            data = {
                "broker_id": self.broker_id,
                "entries": {key: e.to_dict() for key, e in self._entries.items()},
                "block": list(self._block),
                "dirty": sorted(self._dirty),
            }
        tmp = f"{self.snapshot_file}.tmp"
        with open(tmp, "w") as file:
            json.dump(data, file)
        os.replace(tmp, self.snapshot_file)

    def _load_snapshot(self) -> None:
        if self.snapshot_file is None or not os.path.exists(self.snapshot_file):
            return
        try:
            with open(self.snapshot_file) as file:
                data = json.load(file)
            entries = {
                key: Entry.from_dict(value) for key, value in data["entries"].items()
            }
        except (OSError, ValueError, KeyError, TypeError) as error:
            logger.error(
                "Could not read the IPAM snapshot %s: %s", self.snapshot_file, error
            )
            return
        with self._lock:
            self._entries = entries
            self._primary = {key for key, entry in entries.items() if entry.primary}
            self._block = [str(prefix) for prefix in data.get("block", [])]
            self._dirty = {key for key in data.get("dirty", []) if key in entries}
        logger.info(
            "Loaded %d entries of site %s from the snapshot", len(entries), self.name
        )

    def run(self, exit_event: threading.Event, tick: float = 5) -> None:
        """Write the queue every tick and sync every sync_interval."""
        next_sync = self.clock() + self.sync_interval
        while not exit_event.wait(tick):
            try:
                if self.clock() >= next_sync:
                    next_sync = self.clock() + self.sync_interval
                    self.sync()
                else:
                    self.flush()
            except Exception as error:  # noqa: BLE001
                logger.warning("IPAM sync of site %s failed: %s", self.name, error)
            try:
                self.save()
            except OSError as error:
                logger.error("Could not write the IPAM snapshot: %s", error)


def reclaim(
    caches: dict[str, IPAMCache], max_age: float, dry_run: bool, now: float
) -> tuple[set[str], set[str]]:
    """Delete the prefixes of nodes that no site saw for max_age seconds.

    A node counts as seen when any site saw it. Nodes without last_seen get
    last_seen = now and are not deleted. When NetBox fails for one site, the
    sweep stops before it deletes anything.

    Returns:
        The public keys without last_seen, and the public keys to delete.
    """
    seen_by_site = {name: cache.seen() for name, cache in caches.items()}
    unmarked: set[str] = set()
    stale: set[str] = set()
    for pubkey in set().union(*seen_by_site.values()):
        values = [seen[pubkey] for seen in seen_by_site.values() if pubkey in seen]
        if any(value is None for value in values):
            unmarked.add(pubkey)
        elif now - max(v for v in values if v is not None) > max_age:
            stale.add(pubkey)
    logger.info(
        "Prefix reclaim%s: %d nodes without last_seen, %d nodes not seen for %d days",
        " (dry run, no change)" if dry_run else "",
        len(unmarked),
        len(stale),
        max_age // 86400,
    )
    if dry_run:
        return unmarked, stale
    for name, cache in caches.items():
        keys = {key for key in unmarked if seen_by_site[name].get(key, 0) is None}
        if keys:
            cache.mark_seen(keys)
    for pubkey in sorted(stale):
        for name, cache in caches.items():
            if pubkey in seen_by_site[name]:
                cache.release_prefix(pubkey)
    return unmarked, stale
