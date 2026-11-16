"""A set of WireGuard public keys from the configuration and an optional file, reloaded on change."""

import os
import threading
import time
from collections.abc import Callable, Iterable

from wgkex.common import logger


def parse_keys(text: str) -> set[str]:
    """Parses one key per line, ignoring blank lines and comments starting with '#'."""
    keys = set()
    for line in text.splitlines():
        key = line.split("#", 1)[0].strip()
        if key:
            keys.add(key)
    return keys


class KeyList:
    """Thread-safe set of public keys, reloaded from a file when its modification time changes.

    The file is checked at most every check_interval seconds, when the list is queried or reload()
    is called. If the file cannot be read, the previously loaded keys are kept.

    Arguments:
        name: Name used in log messages.
        keys: Keys from the configuration, always contained.
        file: Optional path of a file with one key per line.
        check_interval: Minimum seconds between checks of the file.
        on_change: Called with (added, removed) keys after the file was reloaded (not on the
            initial load).
    """

    def __init__(
        self,
        name: str,
        keys: Iterable[str] = (),
        file: str | None = None,
        check_interval: float = 60,
        on_change: Callable[[set[str], set[str]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._static = {k.strip() for k in keys if k and k.strip()}
        self._file = file
        self._check_interval = check_interval
        self._on_change = on_change
        self._clock = clock
        self._lock = threading.Lock()
        self._file_keys: set[str] = set()
        self._mtime: float | None = None
        self._checked_at: float | None = None
        if file:
            self.reload(force=True, notify=False)

    @property
    def configured(self) -> bool:
        """Whether any keys or a file are configured."""
        return bool(self._static or self._file)

    def __contains__(self, key: str) -> bool:
        if not self.configured:
            return False
        self.reload()
        with self._lock:
            return key in self._static or key in self._file_keys

    def keys(self) -> set[str]:
        self.reload()
        with self._lock:
            return self._static | self._file_keys

    def reload(self, force: bool = False, notify: bool = True) -> None:
        """Reloads the file if it changed and check_interval passed (or force is set)."""
        if not self._file:
            return
        now = self._clock()
        with self._lock:
            if (
                not force
                and self._checked_at is not None
                and now - self._checked_at < self._check_interval
            ):
                return
            self._checked_at = now
            try:
                mtime = os.stat(self._file).st_mtime
                if mtime == self._mtime and not force:
                    return
                with open(self._file, encoding="utf-8") as f:
                    new_keys = parse_keys(f.read())
            except OSError as e:
                if self._mtime is not None or force:
                    logger.error("Could not read %s file: %s", self.name, e)
                self._mtime = None
                return
            old = self._static | self._file_keys
            self._file_keys = new_keys
            self._mtime = mtime
            added = (self._static | new_keys) - old
            removed = old - (self._static | new_keys)
        logger.info(
            "Loaded %s from %s: %d keys (%d added, %d removed)",
            self.name,
            self._file,
            len(new_keys),
            len(added),
            len(removed),
        )
        if notify and self._on_change is not None and (added or removed):
            self._on_change(added, removed)
