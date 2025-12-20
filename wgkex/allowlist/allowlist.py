"""Allowlist manager for pre-approved WireGuard keys."""

import os
import threading
import time
from typing import Dict, List, Optional, Set

import yaml

from wgkex.common import logger


class AllowlistManager:
    """Manages allowlists of pre-approved WireGuard public keys per domain.

    This class loads and manages a YAML file containing allowed public keys
    organized by domain. It supports periodic refresh and validation.

    Attributes:
        allowlist_file: Path to the YAML allowlist file.
        refresh_interval: Time in seconds between automatic refreshes (0 to disable).
        _allowlist: Dictionary mapping domains to sets of allowed public keys.
        _refresh_thread: Background thread for periodic refresh.
        _stop_event: Event to signal the refresh thread to stop.
    """

    def __init__(self, allowlist_file: str, refresh_interval: int = 300):
        """Initialize the AllowlistManager.

        Args:
            allowlist_file: Path to YAML file containing the allowlist.
            refresh_interval: Seconds between automatic refreshes (default: 300, 0 to disable).
        """
        self.allowlist_file = allowlist_file
        self.refresh_interval = refresh_interval
        self._allowlist: Dict[str, Set[str]] = {}
        self._lock = threading.Lock()
        self._refresh_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Initial load
        self.reload()

        # Start refresh thread if interval > 0
        if self.refresh_interval > 0:
            self._start_refresh_thread()

    def reload(self) -> None:
        """Reload the allowlist from the file.

        Raises:
            FileNotFoundError: If the allowlist file doesn't exist.
            yaml.YAMLError: If the YAML file is malformed.
        """
        logger.info(f"Loading allowlist from {self.allowlist_file}")

        if not os.path.exists(self.allowlist_file):
            logger.warning(
                f"Allowlist file {self.allowlist_file} not found. Using empty allowlist."
            )
            with self._lock:
                self._allowlist = {}
            return

        try:
            with open(self.allowlist_file, "r") as f:
                data = yaml.safe_load(f)

            if data is None:
                logger.warning(f"Allowlist file {self.allowlist_file} is empty.")
                with self._lock:
                    self._allowlist = {}
                return

            # Convert lists to sets for faster lookup
            new_allowlist: Dict[str, Set[str]] = {}
            for domain, keys in data.items():
                if not isinstance(keys, list):
                    logger.error(
                        f"Invalid allowlist format for domain {domain}: expected list, got {type(keys)}"
                    )
                    continue
                new_allowlist[domain] = set(keys)
                logger.info(f"Loaded {len(keys)} allowed keys for domain {domain}")

            with self._lock:
                self._allowlist = new_allowlist

            logger.info(
                f"Successfully loaded allowlist with {len(new_allowlist)} domains"
            )

        except yaml.YAMLError as e:
            logger.error(f"Failed to parse allowlist YAML file: {e}")
            raise
        except Exception as e:
            logger.error(f"Failed to load allowlist: {e}")
            raise

    def is_key_allowed(self, domain: str, public_key: str) -> bool:
        """Check if a public key is allowed for a given domain.

        Args:
            domain: The domain to check.
            public_key: The WireGuard public key to check.

        Returns:
            True if the key is allowed, False otherwise.
        """
        with self._lock:
            if domain not in self._allowlist:
                logger.debug(f"Domain {domain} not in allowlist")
                return False

            allowed = public_key in self._allowlist[domain]
            if not allowed:
                logger.debug(f"Key {public_key} not in allowlist for domain {domain}")
            return allowed

    def get_allowed_keys(self, domain: str) -> List[str]:
        """Get all allowed keys for a domain.

        Args:
            domain: The domain to query.

        Returns:
            List of allowed public keys for the domain, empty if domain not found.
        """
        with self._lock:
            return list(self._allowlist.get(domain, set()))

    def get_domains(self) -> List[str]:
        """Get all domains in the allowlist.

        Returns:
            List of domain names.
        """
        with self._lock:
            return list(self._allowlist.keys())

    def _start_refresh_thread(self) -> None:
        """Start the background refresh thread."""
        self._stop_event.clear()
        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()
        logger.info(
            f"Started allowlist refresh thread (interval: {self.refresh_interval}s)"
        )

    def _refresh_loop(self) -> None:
        """Background thread that periodically reloads the allowlist."""
        while not self._stop_event.wait(self.refresh_interval):
            try:
                self.reload()
            except Exception as e:
                logger.error(f"Error reloading allowlist: {e}")

    def stop(self) -> None:
        """Stop the background refresh thread."""
        if self._refresh_thread is not None and self._refresh_thread.is_alive():
            logger.info("Stopping allowlist refresh thread")
            self._stop_event.set()
            self._refresh_thread.join(timeout=5)

    def __del__(self):
        """Cleanup when the object is destroyed."""
        self.stop()
