"""Unit tests for pgw_select.py"""

import unittest

from wgkex.broker.pgw_select import PgwSelector, Selection


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _selector(**kwargs) -> tuple[PgwSelector, FakeClock]:
    clock = FakeClock()
    return PgwSelector(clock=clock, **kwargs), clock


# Nearly equal load, like pgw03 and pgw04 in the field.
_EQUAL = {"pgw03": (14, 13.0), "pgw04": (12, 13.0)}
# pgw03 is clearly above the site mean: 60 > 40 + max(10, 0.2 * 40).
_OVERLOADED = {"pgw03": (60, 40.0), "pgw04": (20, 40.0)}


class PgwSelectorTest(unittest.TestCase):
    def test_sticky_keep(self):
        """A node keeps its pgw when the loads change a little."""
        sel, _ = _selector()
        for loads in (_EQUAL, {"pgw03": (12, 13.0), "pgw04": (14, 13.0)}):
            self.assertEqual(
                sel.select("vie01", loads, "pgw03", "pgw04", standby=False),
                Selection("pgw03", "handshake"),
            )
            self.assertEqual(
                sel.select("vie01", loads, None, "pgw03", standby=False),
                Selection("pgw03", "stored"),
            )

    def test_handshake_wins_over_stored(self):
        """The broker follows the node to the pgw with the fresh handshake."""
        sel, _ = _selector()
        self.assertEqual(
            sel.select("vie01", _EQUAL, "pgw04", "pgw03", standby=False).pgw, "pgw04"
        )

    def test_new_placement_is_least_loaded(self):
        sel, _ = _selector()
        self.assertEqual(
            sel.select("vie01", _EQUAL, None, None, standby=False),
            Selection("pgw04", "new"),
        )

    def test_failover_is_immediate(self):
        """A down pgw is not in loads: the node moves at once, also without move budget."""
        sel, _ = _selector(max_moves=0)
        only_pgw04 = {"pgw04": (12, 12.0)}
        self.assertEqual(
            sel.select("vie01", only_pgw04, None, "pgw03", standby=False),
            Selection("pgw04", "failover"),
        )
        # A handshake on a down pgw does not keep the node there.
        self.assertEqual(
            sel.select("vie01", only_pgw04, "pgw03", "pgw03", standby=False),
            Selection("pgw04", "failover"),
        )
        self.assertIsNone(sel.select("vie01", {}, None, "pgw03", standby=False))

    def test_no_rebalance_within_threshold(self):
        sel, _ = _selector()
        loads = {"pgw03": (49, 40.0), "pgw04": (31, 40.0)}
        for _ in range(10):
            self.assertEqual(
                sel.select("vie01", loads, None, "pgw03", standby=True).pgw, "pgw03"
            )

    def test_rebalance_with_rate_limit(self):
        sel, clock = _selector(max_moves=2, window=120)
        picks = [
            sel.select("vie01", _OVERLOADED, None, "pgw03", standby=True)
            for _ in range(5)
        ]
        self.assertEqual(
            [p.reason for p in picks],
            ["rebalance", "rebalance", "stored", "stored", "stored"],
        )
        self.assertEqual(picks[0].pgw, "pgw04")
        # The budget is per site.
        self.assertEqual(
            sel.select("muc01", {"a": (60, 40.0), "b": (20, 40.0)}, None, "a", True),
            Selection("b", "rebalance"),
        )
        # A new window gives a new budget.
        clock.now += 120
        self.assertEqual(
            sel.select("vie01", _OVERLOADED, None, "pgw03", standby=True).reason,
            "rebalance",
        )

    def test_no_rebalance_after_fresh_handshake(self):
        sel, _ = _selector()
        self.assertEqual(
            sel.select("vie01", _OVERLOADED, "pgw03", "pgw03", standby=True),
            Selection("pgw03", "handshake"),
        )

    def test_no_rebalance_when_target_is_not_below_its_target(self):
        sel, _ = _selector(min_excess=0, tolerance=0)
        loads = {"pgw03": (50, 40.0), "pgw04": (40, 40.0), "pgw05": (30, 40.0)}
        self.assertEqual(
            sel.select("vie01", loads, None, "pgw03", standby=True).pgw, "pgw05"
        )
        balanced = {"pgw03": (41, 40.0), "pgw04": (40, 40.0)}
        self.assertEqual(
            sel.select("vie01", balanced, None, "pgw03", standby=True).pgw, "pgw03"
        )

    def test_standby_tunnels_move_first(self):
        sel, clock = _selector(max_moves=5, window=120)
        # A primary tunnel does not move while the overload is new.
        self.assertEqual(
            sel.select("vie01", _OVERLOADED, None, "pgw03", standby=False).reason,
            "stored",
        )
        clock.now += 60
        self.assertEqual(
            sel.select("vie01", _OVERLOADED, None, "pgw03", standby=True).reason,
            "rebalance",
        )
        # A standby move 60 s ago: primary tunnels still wait.
        clock.now += 100
        self.assertEqual(
            sel.select("vie01", _OVERLOADED, None, "pgw03", standby=False).reason,
            "stored",
        )
        # A full window without standby moves: primary tunnels can move.
        clock.now += 20
        self.assertEqual(
            sel.select("vie01", _OVERLOADED, None, "pgw03", standby=False),
            Selection("pgw04", "rebalance"),
        )

    def test_overload_ends(self):
        """When the overload ends, the wait for primary moves starts again."""
        sel, clock = _selector()
        sel.select("vie01", _OVERLOADED, None, "pgw03", standby=False)
        clock.now += 100
        sel.select("vie01", _EQUAL, None, "pgw03", standby=False)
        clock.now += 100
        self.assertEqual(
            sel.select("vie01", _OVERLOADED, None, "pgw03", standby=False).reason,
            "stored",
        )


if __name__ == "__main__":
    unittest.main()
