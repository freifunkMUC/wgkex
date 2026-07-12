import unittest

import mock

from wgkex.broker.metrics import WorkerMetricsCollection
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
        test_config.workers = config.Workers.from_dict({}, 25)
        config_mock.return_value = test_config

        worker_metrics = WorkerMetricsCollection()
        worker_metrics.update("1", "d", "connected_peers", 20)
        worker_metrics.update("2", "d", "connected_peers", 19)
        worker_metrics.set_online("1")
        worker_metrics.set_online("2")

        worker, diff, connected = worker_metrics.get_best_worker("d")
        self.assertEqual(worker, "2")
        self.assertEqual(diff, -20)  # 19-(1*(20+19))
        self.assertEqual(connected, 19)

    @mock.patch("wgkex.broker.metrics.config.get_config", autospec=True)
    def test_get_best_workers_require_configured_skips_unknown_workers(
        self, config_mock
    ):
        """Verify require_configured only selects workers with a config entry."""
        test_config = mock.MagicMock(spec=config.Config)
        test_config.workers = config.Workers.from_dict({"1": {"id": 5}}, 25)
        config_mock.return_value = test_config

        worker_metrics = WorkerMetricsCollection()
        worker_metrics.update("1", "d", "connected_peers", 20)
        worker_metrics.update("2", "d", "connected_peers", 0)
        worker_metrics.set_online("1")
        worker_metrics.set_online("2")

        results = worker_metrics.get_best_workers("d", [], require_configured=True)
        self.assertEqual([r.name for r in results], ["1"])
        self.assertEqual(results[0].id, 5)

        # Without the flag, unconfigured workers stay selectable (legacy mode).
        results = worker_metrics.get_best_workers("d", [])
        self.assertIn("2", [r.name for r in results])

    @mock.patch("wgkex.broker.metrics.config.get_config", autospec=True)
    def test_get_best_worker_returns_best_imbalanced_domains(self, config_mock):
        """Verify get_best_worker returns the worker with overall least connected clients even if it has more clients on this domain."""
        test_config = mock.MagicMock(spec=config.Config)
        test_config.workers = config.Workers.from_dict({}, 25)
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
        self.assertEqual(diff, -40)  # 30-(1*(25+5+20+20))
        self.assertEqual(connected, 30)

    @mock.patch("wgkex.broker.metrics.config.get_config", autospec=True)
    def test_get_best_worker_weighted_returns_best(self, config_mock):
        """Verify get_best_worker returns the worker with least client differential for weighted workers."""
        test_config = mock.MagicMock(spec=config.Config)
        test_config.workers = config.Workers.from_dict(
            {"1": {"weight": 84}, "2": {"weight": 42}}, 25
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

    @mock.patch("wgkex.broker.metrics.config.get_config", autospec=True)
    def test_get_best_worker_stickyness(self, config_mock):
        """Verify get_best_worker returns the current worker if it is an equally good choice.
        Verify that a slightly worse worker is still chosen if within tolerance."""
        test_config = mock.MagicMock(spec=config.Config)
        test_config.workers = config.Workers.from_dict(
            {"1": {"id": 1, "weight": 50}, "2": {"id": 2, "weight": 50}}, 25
        )
        config_mock.return_value = test_config

        worker_metrics = WorkerMetricsCollection()
        worker_metrics.update("1", "d", "connected_peers", 20)
        worker_metrics.update("2", "d", "connected_peers", 20)
        worker_metrics.set_online("1")
        worker_metrics.set_online("2")

        results = worker_metrics.get_best_workers("d", current_selected_workers=["2"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "2")

        worker_metrics.update("2", "d", "connected_peers", 24)

        results = worker_metrics.get_best_workers("d", current_selected_workers=["2"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "2")

    @mock.patch("wgkex.broker.metrics.config.get_config", autospec=True)
    def test_get_best_worker_stickyness_small_target(self, config_mock):
        """Verify a sticky worker one peer over a small target is kept.

        With small targets the relative tolerance alone is below one peer, so
        any overshoot would evict the sticky worker and make nodes ping-pong
        between workers."""
        test_config = mock.MagicMock(spec=config.Config)
        test_config.workers = config.Workers.from_dict(
            {"1": {"id": 1, "weight": 50}, "2": {"id": 2, "weight": 50}}, 10
        )
        config_mock.return_value = test_config

        worker_metrics = WorkerMetricsCollection()
        # Total 4 peers, target 2 per worker; tolerance 10% of 2 = 0.2 peers.
        worker_metrics.update("1", "d", "connected_peers", 1)
        worker_metrics.update("2", "d", "connected_peers", 3)
        worker_metrics.set_online("1")
        worker_metrics.set_online("2")

        results = worker_metrics.get_best_workers("d", current_selected_workers=["2"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "2")

    @mock.patch("wgkex.broker.metrics.config.get_config", autospec=True)
    def test_overloaded_sticky_worker_is_replaced(self, config_mock):
        test_config = mock.MagicMock(spec=config.Config)
        test_config.workers = config.Workers.from_dict(
            {
                "overloaded": {"id": 1, "weight": 50, "pop": "a"},
                "available": {"id": 2, "weight": 50, "pop": "a"},
                "other-pop": {"id": 3, "weight": 50, "pop": "b"},
                "offline": {"id": 4, "weight": 50, "pop": "a"},
            },
            25,
        )
        config_mock.return_value = test_config

        worker_metrics = WorkerMetricsCollection()
        worker_metrics.update("overloaded", "d", "connected_peers", 100)
        worker_metrics.update("available", "d", "connected_peers", 0)
        worker_metrics.update("other-pop", "d", "connected_peers", 0)
        worker_metrics.update("offline", "d", "connected_peers", 0)
        worker_metrics.set_online("overloaded")
        worker_metrics.set_online("available")
        worker_metrics.set_online("other-pop")

        results = worker_metrics.get_best_workers(
            "d", current_selected_workers=["overloaded"]
        )
        self.assertCountEqual(
            [result.name for result in results], ["available", "other-pop"]
        )

    def test_collection_set_and_total_skip_missing_record(self):
        worker_metrics = WorkerMetricsCollection()
        metrics = worker_metrics.get("worker")
        worker_metrics.set("worker", metrics)
        worker_metrics.data["missing"] = None
        self.assertEqual(worker_metrics.get_total_peer_count(), 0)


if __name__ == "__main__":
    unittest.main()
