"""Selection of the preferred pgw of a node in one site of a multi-site Parker network.

A node keeps its pgw while that pgw is healthy. The broker selects the least-loaded pgw
only for a new placement or when the pgw of the node is not healthy (failover). It moves
a node to another pgw for load reasons only when its pgw is clearly overloaded, and only
a few nodes in each time window, standby-site tunnels first.
"""

import collections
import dataclasses
import threading
import time
from collections.abc import Callable

from wgkex.common import logger


@dataclasses.dataclass(frozen=True)
class Selection:
    """The result of a selection.

    Attributes:
        pgw: The selected pgw.
        reason: Why the broker selected it: handshake, stored, new, failover or rebalance.
    """

    pgw: str
    reason: str


@dataclasses.dataclass
class _SiteState:
    moves: collections.deque = dataclasses.field(default_factory=collections.deque)
    last_standby_move: float | None = None
    # pgw -> time since which it is overloaded without interruption
    overloaded_since: dict[str, float] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class PgwSelector:
    """Selects the preferred pgw of a node in a site.

    Attributes:
        tolerance: A pgw is overloaded when its load is more than
            max(min_excess, tolerance * target) above its weighted target.
        min_excess: See tolerance.
        max_moves: The maximum number of load moves in each site and window.
        window: The rate limit window for load moves (seconds).
        clock: Monotonic time source, overridable for tests.
    """

    tolerance: float = 0.2
    min_excess: int = 10
    max_moves: int = 2
    window: float = 120
    clock: Callable[[], float] = dataclasses.field(default=time.monotonic, repr=False)
    _sites: dict[str, _SiteState] = dataclasses.field(default_factory=dict, repr=False)
    _lock: threading.Lock = dataclasses.field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def select(
        self,
        site: str,
        loads: dict[str, tuple[int, float]],
        handshake_pgw: str | None,
        stored_pgw: str | None,
        standby: bool,
    ) -> Selection | None:
        """Selects the pgw of a node in a site.

        Arguments:
            site: The site name.
            loads: pgw -> (effective peers, weighted target) of the healthy pgws of the site.
            handshake_pgw: The pgw of the site that reported a fresh handshake of the node
                (within the route gate handshake_max_age), if any.
            stored_pgw: The preferred pgw of the site that is stored for the node.
            standby: True if the site is not the primary site of the node.

        Returns:
            The selection, None if no pgw is healthy.
        """
        if not loads:
            return None
        with self._lock:
            now = self.clock()
            state = self._sites.setdefault(site, _SiteState())
            self._update_overload(state, loads, now)

            current, reason = None, ""
            if handshake_pgw in loads:
                current, reason = handshake_pgw, "handshake"
            elif stored_pgw in loads:
                current, reason = stored_pgw, "stored"

            if current is None:
                failover = handshake_pgw is not None or stored_pgw is not None
                return Selection(
                    self._least_loaded(loads), "failover" if failover else "new"
                )

            # A fresh handshake means the node connected to its pgw just now, so do not move it.
            if reason == "handshake" or current not in state.overloaded_since:
                return Selection(current, reason)
            target = self._least_loaded(loads)
            if target == current or loads[target][0] >= loads[target][1]:
                return Selection(current, reason)
            if not self._may_move(state, current, standby, now):
                return Selection(current, reason)

            state.moves.append(now)
            if standby:
                state.last_standby_move = now
            logger.info(
                "Move a %s tunnel in site %s from overloaded %s to %s",
                "standby" if standby else "primary",
                site,
                current,
                target,
            )
            return Selection(target, "rebalance")

    def _update_overload(
        self, state: _SiteState, loads: dict[str, tuple[int, float]], now: float
    ) -> None:
        for pgw, (load, target) in loads.items():
            if load - target > max(self.min_excess, self.tolerance * target):
                state.overloaded_since.setdefault(pgw, now)
            else:
                state.overloaded_since.pop(pgw, None)
        for pgw in [p for p in state.overloaded_since if p not in loads]:
            del state.overloaded_since[pgw]

    def _may_move(self, state: _SiteState, pgw: str, standby: bool, now: float) -> bool:
        while state.moves and now - state.moves[0] >= self.window:
            state.moves.popleft()
        if len(state.moves) >= self.max_moves:
            return False
        if standby:
            return True
        # Move a primary tunnel only after a full window without standby moves,
        # so that the standby tunnels on the pgw go first.
        quiet_since = state.overloaded_since[pgw]
        if state.last_standby_move is not None:
            quiet_since = max(quiet_since, state.last_standby_move)
        return now - quiet_since >= self.window

    @staticmethod
    def _least_loaded(loads: dict[str, tuple[int, float]]) -> str:
        return min(loads, key=lambda pgw: (loads[pgw][0] - loads[pgw][1], pgw))
