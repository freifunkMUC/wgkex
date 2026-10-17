import random
import threading
import unittest
from unittest import mock

from wgkex.broker.metrics import WorkerMetricsCollection
from wgkex.common.mqtt import CONNECTED_PEERS_METRIC
from wgkex.config import config


class TestMetrics(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Give each test a placeholder config
        test_config = config.Config.from_dict(
            {
                "domains": [],
                "domain_prefixes": "",
                "workers": {},
                "mqtt": {"broker_url": "", "username": "", "password": ""},
            }
        )
        mocked_config = mock.create_autospec(spec=test_config, spec_set=True)
        config._parsed_config = mocked_config

    @classmethod
    def tearDownClass(cls) -> None:
        config._parsed_config = None

    def test_set_online_matches_is_online(self):
        """Verify set_online sets worker online and matches result of is_online."""
        worker_metrics = WorkerMetricsCollection()
        worker_metrics.set_online("worker1")

        ret = worker_metrics.get("worker1").is_online()
        self.assertTrue(ret)

    def test_set_offline_matches_is_online(self):
        """Verify set_offline sets worker offline and matches negated result of is_online."""
        worker_metrics = WorkerMetricsCollection()
        worker_metrics.set_offline("worker1")

        ret = worker_metrics.get("worker1").is_online()
        self.assertFalse(ret)

    def test_unkown_is_offline(self):
        """Verify an unkown worker is considered offline."""
        worker_metrics = WorkerMetricsCollection()

        ret = worker_metrics.get("worker1").is_online()
        self.assertFalse(ret)

    def test_set_online_matches_is_online_domain(self):
        """Verify set_online sets worker online and matches result of is_online with domain."""
        worker_metrics = WorkerMetricsCollection()
        worker_metrics.set_online("worker1")
        worker_metrics.update("worker1", "d", "connected_peers", 5)

        ret = worker_metrics.get("worker1").is_online("d")
        self.assertTrue(ret)

    def test_set_online_matches_is_online_offline_domain(self):
        """Verify worker is considered offline if connected_peers for domain is <0."""
        worker_metrics = WorkerMetricsCollection()
        worker_metrics.set_online("worker1")
        worker_metrics.update("worker1", "d", "connected_peers", -1)

        ret = worker_metrics.get("worker1").is_online("d")
        self.assertFalse(ret)

    @mock.patch("wgkex.broker.metrics.config.get_config", autospec=True)
    def test_get_best_worker_returns_best(self, config_mock):
        """Verify get_best_worker returns the worker with least connected clients for equally weighted workers."""
        test_config = mock.MagicMock(spec=config.Config)
        test_config.workers = config.Workers.from_dict({})
        config_mock.return_value = test_config

        worker_metrics = WorkerMetricsCollection()
        worker_metrics.update("1", "d", "connected_peers", 20)
        worker_metrics.update("2", "d", "connected_peers", 19)
        worker_metrics.set_online("1")
        worker_metrics.set_online("2")

        worker, diff, connected = worker_metrics.get_best_worker("d")
        self.assertEqual(worker, "2")
        self.assertEqual(diff, -0.5)  # 19-(0.5*(20+19))
        self.assertEqual(connected, 19)

    @mock.patch("wgkex.broker.metrics.config.get_config", autospec=True)
    def test_get_best_worker_returns_best_imbalanced_domains(self, config_mock):
        """Verify get_best_worker returns the worker with overall least connected clients even if it has more clients on this domain."""
        test_config = mock.MagicMock(spec=config.Config)
        test_config.workers = config.Workers.from_dict({})
        config_mock.return_value = test_config

        worker_metrics = WorkerMetricsCollection()
        worker_metrics.update("1", "domain1", "connected_peers", 25)
        worker_metrics.update("1", "domain2", "connected_peers", 5)
        worker_metrics.update("2", "domain1", "connected_peers", 20)
        worker_metrics.update("2", "domain2", "connected_peers", 20)
        worker_metrics.set_online("1")
        worker_metrics.set_online("2")

        worker, diff, connected = worker_metrics.get_best_worker("domain1")
        self.assertEqual(worker, "1")
        self.assertEqual(diff, -5)  # 30-(0.5*(25+5+20+20))
        self.assertEqual(connected, 30)

    @mock.patch("wgkex.broker.metrics.config.get_config", autospec=True)
    def test_get_best_worker_weighted_returns_best(self, config_mock):
        """Verify get_best_worker returns the worker with least client differential for weighted workers."""
        test_config = mock.MagicMock(spec=config.Config)
        test_config.workers = config.Workers.from_dict(
            {"1": {"weight": 84}, "2": {"weight": 42}}
        )
        config_mock.return_value = test_config

        worker_metrics = WorkerMetricsCollection()
        worker_metrics.update("1", "d", "connected_peers", 21)
        worker_metrics.update("2", "d", "connected_peers", 19)
        worker_metrics.set_online("1")
        worker_metrics.set_online("2")

        worker, _, _ = worker_metrics.get_best_worker("d")
        config_mock.assert_called()
        self.assertEqual(worker, "1")

    def test_get_best_worker_no_worker_online_returns_none(self):
        """Verify get_best_worker returns None if there is no online worker."""
        worker_metrics = WorkerMetricsCollection()
        worker_metrics.update("1", "d", "connected_peers", 20)
        worker_metrics.update("2", "d", "connected_peers", 19)
        worker_metrics.set_offline("1")
        worker_metrics.set_offline("2")

        worker, _, _ = worker_metrics.get_best_worker("d")
        self.assertIsNone(worker)

    def test_get_best_worker_no_worker_registered_returns_none(self):
        """Verify get_best_worker returns None if there is no online worker."""
        worker_metrics = WorkerMetricsCollection()

        worker, _, _ = worker_metrics.get_best_worker("d")
        self.assertIsNone(worker)


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestLoadBalancing(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("wgkex.broker.metrics.config.get_config", autospec=True)
        self.config_mock = patcher.start()
        self.addCleanup(patcher.stop)
        self.set_weights({})
        self.clock = FakeClock()

    def set_weights(self, weights: dict) -> None:
        test_config = mock.MagicMock(spec=config.Config)
        test_config.workers = config.Workers.from_dict(weights)
        self.config_mock.return_value = test_config

    def collection(self, seed: int = 0, **kwargs) -> WorkerMetricsCollection:
        return WorkerMetricsCollection(
            clock=self.clock, rng=random.Random(seed), **kwargs
        )

    @staticmethod
    def add_worker(wm, worker, peers, domains=("d",)):
        for domain in domains:
            wm.update(worker, domain, CONNECTED_PEERS_METRIC, peers)
        wm.set_online(worker)

    def test_stale_metrics_mark_domain_offline(self):
        """A worker without a recent connected peers metric is offline for that domain."""
        wm = self.collection(metrics_max_age=180)
        self.add_worker(wm, "w1", 10, domains=("d", "e"))
        self.clock.now += 100
        wm.update("w1", "e", CONNECTED_PEERS_METRIC, 10)
        self.clock.now += 100
        self.assertEqual(wm.get_best_worker("d")[0], None)
        self.assertEqual(wm.get_best_worker("e")[0], "w1")
        # Fresh metrics bring the domain back
        wm.update("w1", "d", CONNECTED_PEERS_METRIC, 10)
        self.assertEqual(wm.get_best_worker("d")[0], "w1")

    def test_stale_metrics_disabled(self):
        """With metrics_max_age 0, metrics never become stale."""
        wm = self.collection(metrics_max_age=0)
        self.add_worker(wm, "w1", 10)
        self.clock.now += 100000
        self.assertEqual(wm.get_best_worker("d")[0], "w1")

    def test_stale_metrics_change_topology(self):
        """A worker going stale counts as topology change for rebalancing."""
        wm = self.collection(metrics_max_age=180)
        self.add_worker(wm, "w1", 10)
        self.add_worker(wm, "w2", 10, domains=("d", "e"))
        self.clock.now += 120
        wm.update("w2", "d", CONNECTED_PEERS_METRIC, 10)
        self.assertTrue(wm.is_topology_stable(100))
        self.clock.now += 100
        # w1/d and w2/e went stale
        self.assertFalse(wm.is_topology_stable(1))

    def test_unknown_worker_gets_average_weight(self):
        """Workers missing from the config get the average configured weight."""
        self.set_weights({"w1": {"weight": 10}, "w2": {"weight": 30}})
        wm = self.collection()
        self.add_worker(wm, "w1", 0)
        self.add_worker(wm, "w2", 0)
        self.add_worker(wm, "w3", 0)
        for i in range(400):
            worker = wm.get_best_worker("d")[0]
            wm.record_assignment(worker, "d", f"key{i}")
        # Weights 10:30:20
        self.assertAlmostEqual(wm.get_effective_peer_count("w1"), 400 / 6, delta=2)
        self.assertAlmostEqual(wm.get_effective_peer_count("w3"), 400 // 3, delta=2)

    def test_pending_assignments_count_towards_load(self):
        """Pending assignments increase the effective load until reflected by metrics."""
        wm = self.collection()
        self.add_worker(wm, "1", 10)
        wm.record_assignment("1", "d", "key1")
        wm.record_assignment("1", "d", "key2")
        self.assertEqual(wm.get_effective_peer_count("1"), 12)

    def test_pending_assignment_reassigned(self):
        """A re-keying peer is only pending on the worker it was last assigned to."""
        wm = self.collection()
        self.add_worker(wm, "1", 0)
        self.add_worker(wm, "2", 0)
        wm.record_assignment("1", "d", "key")
        wm.record_assignment("2", "d", "key")
        self.assertEqual(wm.get_pending_count("1"), 0)
        self.assertEqual(wm.get_pending_count("2"), 1)

    def test_metric_update_keeps_recent_pending(self):
        """A metric update does not erase assignments younger than the settle time."""
        wm = self.collection(pending_settle_time=60)
        self.add_worker(wm, "1", 10)
        wm.record_assignment("1", "d", "key")
        self.clock.now += 30
        wm.update("1", "d", CONNECTED_PEERS_METRIC, 10)
        self.assertEqual(wm.get_effective_peer_count("1"), 11)

    def test_metric_update_drops_settled_pending(self):
        """A metric update drops assignments old enough to be included in it."""
        wm = self.collection(pending_settle_time=60)
        self.add_worker(wm, "1", 10)
        wm.record_assignment("1", "d", "key")
        self.clock.now += 61
        wm.update("1", "d", CONNECTED_PEERS_METRIC, 11)
        self.assertEqual(wm.get_effective_peer_count("1"), 11)

    def test_metric_update_other_domain_keeps_pending(self):
        """Metrics of another domain don't affect pending assignments."""
        wm = self.collection(pending_settle_time=60)
        self.add_worker(wm, "1", 10, domains=("d", "e"))
        wm.record_assignment("1", "d", "key")
        self.clock.now += 61
        wm.update("1", "e", CONNECTED_PEERS_METRIC, 10)
        self.assertEqual(wm.get_pending_count("1"), 1)

    def test_pending_expires_after_ttl(self):
        """Pending assignments expire after the TTL even without metric updates."""
        wm = self.collection(pending_ttl=300)
        self.add_worker(wm, "1", 10)
        wm.record_assignment("1", "d", "key")
        self.clock.now += 301
        self.assertEqual(wm.get_effective_peer_count("1"), 10)

    def test_offline_worker_drops_pending(self):
        """Pending assignments of a worker going offline are dropped."""
        wm = self.collection()
        self.add_worker(wm, "1", 10)
        wm.record_assignment("1", "d", "key")
        wm.set_offline("1")
        self.assertEqual(wm.get_pending_count("1"), 0)

    def test_burst_is_spread_with_pending_assignments(self):
        """A burst of key exchanges between metric updates (e.g. after a gateway went down)
        is spread over the remaining workers instead of all landing on one."""
        wm = self.collection()
        self.add_worker(wm, "1", 100)
        self.add_worker(wm, "2", 100)
        self.add_worker(wm, "3", 100)
        counts = {"1": 0, "2": 0, "3": 0}
        for i in range(300):
            worker, _, _ = wm.get_best_worker("d")
            wm.record_assignment(worker, "d", f"key{i}")
            counts[worker] += 1
        for count in counts.values():
            self.assertAlmostEqual(count, 100, delta=5)

    def test_burst_respects_weights(self):
        """Assignments follow the configured worker weights."""
        self.set_weights({"1": {"weight": 3}, "2": {"weight": 1}})
        wm = self.collection()
        self.add_worker(wm, "1", 0)
        self.add_worker(wm, "2", 0)
        counts = {"1": 0, "2": 0}
        for i in range(400):
            worker, _, _ = wm.get_best_worker("d")
            wm.record_assignment(worker, "d", f"key{i}")
            counts[worker] += 1
        self.assertAlmostEqual(counts["1"], 300, delta=5)
        self.assertAlmostEqual(counts["2"], 100, delta=5)

    def test_rebooted_worker_is_filled_first(self):
        """A worker that comes back empty gets new clients until it reaches its target."""
        wm = self.collection()
        self.add_worker(wm, "1", 0)
        self.add_worker(wm, "2", 500)
        self.add_worker(wm, "3", 500)
        for i in range(100):
            worker, _, _ = wm.get_best_worker("d")
            self.assertEqual(worker, "1")
            wm.record_assignment(worker, "d", f"key{i}")

    def test_selection_is_not_deterministic_when_balanced(self):
        """Balanced workers are chosen randomly, so multiple brokers don't herd onto one worker."""
        chosen = set()
        for seed in range(20):
            wm = self.collection(seed=seed)
            self.add_worker(wm, "1", 50)
            self.add_worker(wm, "2", 50)
            chosen.add(wm.get_best_worker("d")[0])
        self.assertSetEqual(chosen, {"1", "2"})

    def test_location_preferred_within_tolerance(self):
        """Workers in the requested location are preferred while not too far above target."""
        self.set_weights(
            {
                "1": {"weight": 1, "location": "muc"},
                "2": {"weight": 1, "location": "fra"},
            }
        )
        wm = self.collection()
        self.add_worker(wm, "1", 100)
        self.add_worker(wm, "2", 100)
        # target 100 each, tolerance max(10, 0.1*target) -> fra may get up to 110 peers
        for i in range(10):
            worker, _, _ = wm.get_best_worker("d", location="fra")
            self.assertEqual(worker, "2")
            wm.record_assignment(worker, "d", f"key{i}")
        # fra is preferred while its load stays within 1.1 * target, afterwards it stays there
        for i in range(10, 40):
            worker, _, _ = wm.get_best_worker("d", location="fra")
            wm.record_assignment(worker, "d", f"key{i}")
            fra = wm.get_effective_peer_count("2")
            target = (fra + wm.get_effective_peer_count("1")) / 2
            self.assertLessEqual(fra, target + max(10, 0.1 * target) + 1)
        self.assertGreater(wm.get_pending_count("1"), 5)
        self.assertGreater(wm.get_pending_count("1"), 0)

    def test_location_overloaded_falls_back(self):
        self.set_weights(
            {
                "1": {"weight": 1, "location": "muc"},
                "2": {"weight": 1, "location": "fra"},
            }
        )
        wm = self.collection()
        self.add_worker(wm, "1", 50)
        self.add_worker(wm, "2", 150)
        for seed in range(20):
            wm.rng = random.Random(seed)
            self.assertEqual(wm.get_best_worker("d", location="fra")[0], "1")

    def test_unknown_location_normal_selection(self):
        self.set_weights({"1": {"weight": 1, "location": "muc"}, "2": {"weight": 1}})
        wm = self.collection()
        self.add_worker(wm, "1", 100)
        self.add_worker(wm, "2", 0)
        for seed in range(20):
            wm.rng = random.Random(seed)
            self.assertEqual(wm.get_best_worker("d", location="ber")[0], "2")
            self.assertEqual(wm.get_best_worker("d")[0], "2")

    def test_location_respects_tolerance_settings(self):
        self.set_weights(
            {
                "1": {"weight": 1, "location": "muc"},
                "2": {"weight": 1, "location": "fra"},
            }
        )
        wm = self.collection()
        self.add_worker(wm, "1", 0)
        self.add_worker(wm, "2", 200)
        # target 100: with tolerance 1.0 fra may get up to 200 + 1 is over -> fallback
        self.assertEqual(
            wm.get_best_worker("d", location="fra", location_tolerance=1.0)[0], "1"
        )
        self.assertEqual(
            wm.get_best_worker(
                "d", location="fra", location_tolerance=1.5, location_min_excess=0
            )[0],
            "2",
        )

    def test_get_pending_worker(self):
        wm = self.collection()
        self.add_worker(wm, "1", 0)
        self.assertIsNone(wm.get_pending_worker("d", "key"))
        wm.record_assignment("1", "d", "key")
        self.assertEqual(wm.get_pending_worker("d", "key"), "1")
        self.assertIsNone(wm.get_pending_worker("other", "key"))

    def test_get_best_worker_only_online_for_domain(self):
        """Workers offline for the domain are never chosen."""
        wm = self.collection()
        self.add_worker(wm, "1", 1000)
        self.add_worker(wm, "2", 0, domains=("other",))
        wm.update("2", "d", CONNECTED_PEERS_METRIC, -1)
        for seed in range(20):
            wm.rng = random.Random(seed)
            self.assertEqual(wm.get_best_worker("d")[0], "1")

    def test_get_best_worker_eligible(self):
        """Workers rejected by the eligible filter are never chosen."""
        wm = self.collection()
        self.add_worker(wm, "1", 1000)
        self.add_worker(wm, "2", 0)
        worker, _, _ = wm.get_best_worker("d", eligible=lambda w: w != "2")
        self.assertEqual(worker, "1")
        worker, _, _ = wm.get_best_worker("d", eligible=lambda w: False)
        self.assertIsNone(worker)

    def test_concurrent_assignments_are_thread_safe(self):
        """Concurrent selection, assignment and metric updates don't raise or lose assignments."""
        wm = WorkerMetricsCollection(pending_settle_time=1000)
        for w in ("1", "2", "3"):
            self.add_worker(wm, w, 0)
        errors = []

        def assign(n):
            try:
                for i in range(200):
                    worker, _, _ = wm.get_best_worker("d")
                    wm.record_assignment(worker, "d", f"key{n}-{i}")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def update_metrics():
            try:
                for _ in range(200):
                    for w in ("1", "2", "3"):
                        wm.update(w, "d", CONNECTED_PEERS_METRIC, 0)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=assign, args=(n,)) for n in range(4)]
        threads.append(threading.Thread(target=update_metrics))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertListEqual(errors, [])
        self.assertEqual(sum(wm.get_pending_count(w) for w in ("1", "2", "3")), 800)

    def hints(self, wm, **kwargs):
        args = {
            "domains": ["d"],
            "tolerance": 0.1,
            "min_excess": 10,
            "stable_time": 600,
        }
        args.update(kwargs)
        return wm.compute_rebalance_hints(**args)

    def test_rebalance_hints_for_overloaded_worker(self):
        """An overloaded worker gets a hint with its excess over the weighted target."""
        wm = self.collection()
        self.add_worker(wm, "1", 100)
        self.add_worker(wm, "2", 400)
        self.add_worker(wm, "3", 700)
        self.clock.now += 600
        hints = self.hints(wm)
        self.assertSetEqual(set(hints), {"3"})
        self.assertEqual(hints["3"].excess, 300)
        self.assertListEqual(hints["3"].domains, ["d"])
        self.assertDictEqual(hints["3"].to_dict(), {"excess": 300, "domains": ["d"]})

    def test_rebalance_hints_respect_weights(self):
        """Weighted targets are used for the excess computation."""
        self.set_weights({"1": {"weight": 3}, "2": {"weight": 1}})
        wm = self.collection()
        self.add_worker(wm, "1", 300)
        self.add_worker(wm, "2", 100)
        self.clock.now += 600
        self.assertDictEqual(self.hints(wm), {})

    def test_rebalance_hints_within_tolerance(self):
        """No hints are produced when the excess is within the tolerance."""
        wm = self.collection()
        self.add_worker(wm, "1", 95)
        self.add_worker(wm, "2", 105)
        self.clock.now += 600
        self.assertDictEqual(self.hints(wm), {})

    def test_rebalance_hints_wait_for_stable_fleet(self):
        """No hints are produced shortly after a worker came online (hysteresis)."""
        wm = self.collection()
        self.add_worker(wm, "2", 600)
        self.clock.now += 3600
        self.add_worker(wm, "1", 0)
        self.clock.now += 599
        self.assertDictEqual(self.hints(wm), {})
        self.clock.now += 1
        self.assertSetEqual(set(self.hints(wm)), {"2"})

    def test_rebalance_hints_wait_after_broker_start(self):
        """No hints are produced right after the broker started."""
        wm = self.collection()
        self.add_worker(wm, "1", 0)
        self.add_worker(wm, "2", 600)
        self.assertDictEqual(self.hints(wm), {})

    def test_rebalance_hints_only_domains_with_underloaded_worker(self):
        """Hints only allow evictions in domains where an underloaded worker is online."""
        wm = self.collection()
        self.add_worker(wm, "1", 0, domains=("a",))
        self.add_worker(wm, "2", 300, domains=("a", "b"))
        self.clock.now += 600
        hints = self.hints(wm, domains=["a", "b"])
        self.assertListEqual(hints["2"].domains, ["a"])

    def test_rebalance_hints_include_pending(self):
        """Pending assignments count towards the load for rebalancing."""
        wm = self.collection(pending_ttl=10000)
        self.add_worker(wm, "1", 100)
        self.add_worker(wm, "2", 100)
        self.clock.now += 600
        for i in range(100):
            wm.record_assignment("2", "d", f"key{i}")
        self.assertEqual(self.hints(wm)["2"].excess, 50)

    def test_draining_worker_gets_no_new_peers(self):
        """Draining workers are skipped while another worker is online, even if underloaded."""
        wm = self.collection()
        self.add_worker(wm, "1", 0)
        self.add_worker(wm, "2", 500)
        wm.set_draining("1", True)
        for seed in range(20):
            wm.rng = random.Random(seed)
            self.assertEqual(wm.get_best_worker("d")[0], "2")

    def test_all_draining_still_returns_worker(self):
        """If all workers are draining, a client still gets an endpoint."""
        wm = self.collection()
        self.add_worker(wm, "1", 10)
        wm.set_draining("1", True)
        self.assertEqual(wm.get_best_worker("d")[0], "1")

    def test_offline_resets_draining(self):
        wm = self.collection()
        self.add_worker(wm, "1", 10)
        wm.set_draining("1", True)
        wm.set_offline("1")
        self.assertFalse(wm.is_draining("1"))

    def test_drain_hints(self):
        """Draining workers are asked to shed all peers at the drain rate, without waiting for stability."""
        wm = self.collection()
        self.add_worker(wm, "1", 100, domains=("a", "b"))
        self.add_worker(wm, "2", 50, domains=("a",))
        wm.set_draining("1", True)
        self.assertDictEqual(self.hints(wm), {})
        hints = self.hints(wm, domains=["a", "b"], drain_rate=0.2)
        self.assertSetEqual(set(hints), {"1"})
        # Only domain a has somewhere else to go
        self.assertDictEqual(
            hints["1"].to_dict(),
            {"excess": 100, "domains": ["a"], "max_evictions_ratio": 0.2},
        )

    def test_drain_hints_need_active_worker(self):
        wm = self.collection()
        self.add_worker(wm, "1", 100)
        self.add_worker(wm, "2", 100)
        wm.set_draining("1", True)
        wm.set_draining("2", True)
        self.assertDictEqual(self.hints(wm, drain_rate=0.2), {})

    def test_rebalance_ignores_draining_workers(self):
        """Normal rebalancing is computed among the workers which are not draining."""
        wm = self.collection()
        self.add_worker(wm, "1", 100)
        self.add_worker(wm, "2", 100)
        self.add_worker(wm, "3", 0)
        wm.set_draining("3", True)
        self.clock.now += 600
        self.assertDictEqual(self.hints(wm), {})
        self.assertSetEqual(set(self.hints(wm, drain_rate=0.1)), set())

    def test_snapshot(self):
        """The snapshot contains load, weighted targets and per-domain state of all workers."""
        self.set_weights({"1": {"weight": 3}, "2": {"weight": 1}})
        wm = self.collection(metrics_max_age=180)
        self.add_worker(wm, "1", 10, domains=("d", "e"))
        self.add_worker(wm, "2", 20)
        self.add_worker(wm, "3", 5)
        wm.set_draining("3", True)
        wm.record_assignment("2", "d", "k1")
        wm.update("4", "d", CONNECTED_PEERS_METRIC, 7)
        self.clock.now += 100
        wm.update("1", "d", CONNECTED_PEERS_METRIC, 10)

        snap = {s.worker: s for s in wm.snapshot()}
        self.assertListEqual(sorted(snap), ["1", "2", "3", "4"])
        # Active workers 1 and 2 have 20 + 21 effective peers, split 3:1
        self.assertEqual(snap["1"].effective_peers, 20)
        self.assertAlmostEqual(snap["1"].target, 41 * 0.75)
        self.assertEqual((snap["2"].pending, snap["2"].effective_peers), (1, 21))
        self.assertAlmostEqual(snap["2"].target, 41 * 0.25)
        self.assertTrue(snap["3"].draining)
        self.assertEqual(snap["3"].target, 0)
        self.assertFalse(snap["4"].online)
        self.assertEqual(snap["4"].target, 0)
        self.assertDictEqual(snap["1"].domains, {"d": (True, 10), "e": (True, 10)})
        # Metrics of domain "e" are stale after another 100s
        self.clock.now += 100
        self.assertDictEqual(
            {s.worker: s for s in wm.snapshot()}["1"].domains,
            {"d": (True, 10), "e": (False, 10)},
        )


if __name__ == "__main__":
    unittest.main()
