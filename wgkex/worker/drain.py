"""Drain mode: stop getting new peers and shed the existing ones before planned maintenance.

A worker is draining while draining was requested via SIGUSR1 (cancelled by SIGUSR2), or while the
configured drain file exists. A draining worker publishes status 2, which brokers from before drain
mode treat like 1 (online). New brokers stop assigning new peers to it and, if active rebalancing is
enabled, ask it to evict its peers at the configured drain rate.
"""

import os
import threading
from collections.abc import Callable

from wgkex.common import logger
from wgkex.common.mqtt import STATUS_DRAINING, STATUS_ONLINE


class DrainState:
    """Drain state of this worker.

    Uses no locks (single attribute assignments are atomic), as request() is called from signal
    handlers, which run in the main thread and could otherwise deadlock.

    Arguments:
        file: Optional path of a file, the worker is draining while it exists.
        exists: Function checking whether a path exists, overridable for tests.
    """

    def __init__(
        self, file: str | None = None, exists: Callable[[str], bool] = os.path.exists
    ) -> None:
        self._file = file
        self._exists = exists
        self._requested = False
        # Set whenever the state might have changed, wakes up the watcher
        self.changed = threading.Event()

    def configure(self, file: str | None) -> None:
        self._file = file
        self.changed.set()

    def request(self, draining: bool) -> None:
        """Starts or stops draining on request (e.g. by a signal). Safe to call from signal handlers."""
        self._requested = draining
        self.changed.set()

    def is_draining(self) -> bool:
        requested, file = self._requested, self._file
        if requested:
            return True
        if file:
            try:
                return self._exists(file)
            except OSError:
                return False
        return False

    def status(self) -> int:
        """Returns the worker status to publish while the worker is ready."""
        return STATUS_DRAINING if self.is_draining() else STATUS_ONLINE


drain_state = DrainState()


def watch(
    exit_event: threading.Event,
    publish_status: Callable[[int], bool],
    check_interval: float,
    state: DrainState = drain_state,
) -> None:
    """Publishes the worker status whenever the drain state changes, until exit_event is set.

    Arguments:
        exit_event: Stops the watcher when set.
        publish_status: Publishes a status, returns False if it could not be published yet
            (e.g. while the worker is not announced), in which case it is retried.
        check_interval: Seconds between checks of the drain file.
        state: The drain state to watch.
    """
    published: int | None = None
    while not exit_event.is_set():
        state.changed.clear()
        status = state.status()
        if status != published:
            try:
                if publish_status(status):
                    published = status
            except Exception as e:  # noqa: BLE001
                logger.error("Could not publish worker status: %s", e)
        state.changed.wait(check_interval)
