"""Blacklist handling for WireGuard keys."""

import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

import yaml

from wgkex.common import logger


@dataclass
class BlacklistEntry:
    """A blacklisted key entry.

    Attributes:
        key: The blacklisted WireGuard public key.
        reason: Optional reason for blacklisting.
    """

    key: str
    reason: Optional[str] = None


class Blacklist:
    """Manages blacklisted WireGuard keys.

    This class loads a blacklist from a YAML file and provides methods to check
    if a key is blacklisted. It optionally monitors the file for changes and
    automatically reloads.

    Attributes:
        filepath: Path to the blacklist YAML file.
        auto_reload: Whether to automatically reload on file changes.
    """

    def __init__(self, filepath: str, auto_reload: bool = True):
        """Initialize the blacklist.

        Arguments:
            filepath: Path to the blacklist YAML file.
            auto_reload: Whether to monitor file for changes and auto-reload.
        """
        self.filepath = filepath
        self.auto_reload = auto_reload
        self._blacklist: Dict[str, BlacklistEntry] = {}
        self._lock = threading.RLock()
        self._last_mtime: Optional[float] = None
        self._stop_monitoring = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

        # Initial load
        self._load_blacklist()

        # Start monitoring thread if auto-reload is enabled
        if self.auto_reload:
            self._start_monitoring()

    def _load_blacklist(self) -> None:
        """Load blacklist from the YAML file."""
        if not os.path.exists(self.filepath):
            logger.info(f"Blacklist file not found: {self.filepath}")
            with self._lock:
                self._blacklist = {}
                self._last_mtime = None
            return

        try:
            mtime = os.path.getmtime(self.filepath)
            with open(self.filepath, "r") as f:
                data = yaml.safe_load(f)

            if data is None:
                data = []

            new_blacklist: Dict[str, BlacklistEntry] = {}

            # Handle both list and dict formats
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, str):
                        # Simple string format
                        new_blacklist[item] = BlacklistEntry(key=item)
                    elif isinstance(item, dict):
                        # Dict format with optional reason
                        for key, value in item.items():
                            reason = None
                            if isinstance(value, dict) and "reason" in value:
                                reason = value["reason"]
                            elif isinstance(value, str):
                                reason = value
                            new_blacklist[key] = BlacklistEntry(key=key, reason=reason)
            elif isinstance(data, dict):
                # Direct dict format
                for key, value in data.items():
                    reason = None
                    if isinstance(value, dict) and "reason" in value:
                        reason = value["reason"]
                    elif isinstance(value, str):
                        reason = value
                    new_blacklist[key] = BlacklistEntry(key=key, reason=reason)

            with self._lock:
                self._blacklist = new_blacklist
                self._last_mtime = mtime

            logger.info(
                f"Loaded {len(new_blacklist)} blacklisted key(s) from {self.filepath}"
            )

        except yaml.YAMLError as e:
            logger.error(f"Failed to parse blacklist YAML: {e}")
        except Exception as e:
            logger.error(f"Failed to load blacklist: {e}")

    def _start_monitoring(self) -> None:
        """Start the file monitoring thread."""
        self._stop_monitoring.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_file, daemon=True, name="BlacklistMonitor"
        )
        self._monitor_thread.start()

    def _monitor_file(self) -> None:
        """Monitor the blacklist file for changes and reload if modified."""
        while not self._stop_monitoring.is_set():
            try:
                if os.path.exists(self.filepath):
                    mtime = os.path.getmtime(self.filepath)
                    with self._lock:
                        last_mtime = self._last_mtime

                    if last_mtime is None or mtime > last_mtime:
                        logger.info(f"Blacklist file changed, reloading: {self.filepath}")
                        self._load_blacklist()
                elif self._last_mtime is not None:
                    # File was deleted
                    logger.info(f"Blacklist file deleted: {self.filepath}")
                    with self._lock:
                        self._blacklist = {}
                        self._last_mtime = None

            except Exception as e:
                logger.error(f"Error monitoring blacklist file: {e}")

            # Check every 10 seconds
            self._stop_monitoring.wait(10)

    def is_blacklisted(self, key: str) -> bool:
        """Check if a key is blacklisted.

        Arguments:
            key: The WireGuard public key to check.

        Returns:
            True if the key is blacklisted, False otherwise.
        """
        with self._lock:
            return key in self._blacklist

    def get_reason(self, key: str) -> Optional[str]:
        """Get the blacklist reason for a key.

        Arguments:
            key: The WireGuard public key.

        Returns:
            The blacklist reason if available, None otherwise.
        """
        with self._lock:
            entry = self._blacklist.get(key)
            return entry.reason if entry else None

    def stop(self) -> None:
        """Stop the file monitoring thread."""
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._stop_monitoring.set()
            self._monitor_thread.join(timeout=5)
            if self._monitor_thread.is_alive():
                logger.warning(
                    "Blacklist monitor thread did not stop within timeout, "
                    "continuing as daemon"
                )

    def reload(self) -> None:
        """Manually reload the blacklist from file."""
        self._load_blacklist()


# Global blacklist instance and lock
_blacklist_instance: Optional[Blacklist] = None
_blacklist_lock = threading.Lock()


def init_blacklist(filepath: str, auto_reload: bool = True) -> None:
    """Initialize the global blacklist instance.

    Arguments:
        filepath: Path to the blacklist YAML file.
        auto_reload: Whether to monitor file for changes and auto-reload.
    """
    global _blacklist_instance
    with _blacklist_lock:
        if _blacklist_instance is not None:
            _blacklist_instance.stop()
        _blacklist_instance = Blacklist(filepath, auto_reload)


def get_blacklist() -> Optional[Blacklist]:
    """Get the global blacklist instance.

    Returns:
        The blacklist instance, or None if not initialized.
    """
    with _blacklist_lock:
        return _blacklist_instance
