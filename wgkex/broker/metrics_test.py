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
        test_config.workers = config.Workers.from_dict({})
        config_mock.return_value = test_config

        worker_metrics = WorkerMetricsCollection()
        worker_metrics.update("1", "d", "connected_peers", 20)
        worker_metrics.update("2", "d", "connected_peers", 19)
        worker_metrics.set_online("1")
        worker_metrics.set_online("2")

        (worker, diff, connected) = worker_metrics.get_best_worker("d")
        self.assertEqual(worker, "2")
        self.assertEqual(diff, -20)  # 19-(1*(20+19))
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

        (worker, diff, connected) = worker_metrics.get_best_worker("domain1")
        self.assertEqual(worker, "1")
        self.assertEqual(diff, -40)  # 30-(1*(25+5+20+20))
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

        (worker, _, _) = worker_metrics.get_best_worker("d")
        config_mock.assert_called()
        self.assertEqual(worker, "1")

    def test_get_best_worker_no_worker_online_returns_none(self):
        """Verify get_best_worker returns None if there is no online worker."""
        worker_metrics = WorkerMetricsCollection()
        worker_metrics.update("1", "d", "connected_peers", 20)
        worker_metrics.update("2", "d", "connected_peers", 19)
        worker_metrics.set_offline("1")
        worker_metrics.set_offline("2")

        (worker, _, _) = worker_metrics.get_best_worker("d")
        self.assertIsNone(worker)

    def test_get_best_worker_no_worker_registered_returns_none(self):
        """Verify get_best_worker returns None if there is no online worker."""
        worker_metrics = WorkerMetricsCollection()

        (worker, _, _) = worker_metrics.get_best_worker("d")
        self.assertIsNone(worker)

    @mock.patch("wgkex.broker.metrics.config.get_config", autospec=True)
    def test_get_best_worker_with_location_filter(self, config_mock):
        """Verify get_best_worker filters by location when specified."""
        test_config = mock.MagicMock(spec=config.Config)
        test_workers = config.Workers.from_dict(
            {
                "worker1": {"weight": 50, "location": "MUC"},
                "worker2": {"weight": 50, "location": "Vienna"},
            }
        )
        test_config.workers = test_workers
        config_mock.return_value = test_config

        worker_metrics = WorkerMetricsCollection()
        worker_metrics.update("worker1", "d", "connected_peers", 10)
        worker_metrics.update("worker2", "d", "connected_peers", 5)
        worker_metrics.set_online("worker1")
        worker_metrics.set_online("worker2")

        (worker, _, _) = worker_metrics.get_best_worker("d", location="MUC")
        self.assertEqual(worker, "worker1")

    @mock.patch("wgkex.broker.metrics.config.get_config", autospec=True)
    def test_get_best_worker_location_fallback(self, config_mock):
        """Verify get_best_worker falls back to all workers if location has no online workers."""
        test_config = mock.MagicMock(spec=config.Config)
        test_workers = config.Workers.from_dict(
            {
                "worker1": {"weight": 50, "location": "MUC"},
                "worker2": {"weight": 50, "location": "Vienna"},
            }
        )
        test_config.workers = test_workers
        config_mock.return_value = test_config

        worker_metrics = WorkerMetricsCollection()
        worker_metrics.update("worker1", "d", "connected_peers", 10)
        worker_metrics.update("worker2", "d", "connected_peers", 5)
        worker_metrics.set_online("worker1")
        worker_metrics.set_offline("worker2")

        # Request Vienna location, but Vienna worker is offline, should fall back to worker1
        (worker, _, _) = worker_metrics.get_best_worker("d", location="Vienna")
        self.assertEqual(worker, "worker1")

    @mock.patch("wgkex.broker.metrics.config.get_config", autospec=True)
    def test_get_best_worker_unknown_location_fallback(self, config_mock):
        """Verify get_best_worker falls back to all workers for unknown location."""
        test_config = mock.MagicMock(spec=config.Config)
        test_workers = config.Workers.from_dict(
            {
                "worker1": {"weight": 50, "location": "MUC"},
                "worker2": {"weight": 50, "location": "Vienna"},
            }
        )
        test_config.workers = test_workers
        config_mock.return_value = test_config

        worker_metrics = WorkerMetricsCollection()
        worker_metrics.update("worker1", "d", "connected_peers", 10)
        worker_metrics.update("worker2", "d", "connected_peers", 5)
        worker_metrics.set_online("worker1")
        worker_metrics.set_online("worker2")

        # Request non-existent Berlin location, should fall back to best available
        (worker, _, _) = worker_metrics.get_best_worker("d", location="Berlin")
        self.assertEqual(worker, "worker2")  # worker2 has fewer peers

    @mock.patch("wgkex.broker.metrics.config.get_config", autospec=True)
    def test_get_best_worker_location_load_balancing(self, config_mock):
        """Verify get_best_worker maintains load balancing within location."""
        test_config = mock.MagicMock(spec=config.Config)
        test_workers = config.Workers.from_dict(
            {
                "worker1": {"weight": 50, "location": "MUC"},
                "worker2": {"weight": 50, "location": "MUC"},
                "worker3": {"weight": 50, "location": "Vienna"},
            }
        )
        test_config.workers = test_workers
        config_mock.return_value = test_config

        worker_metrics = WorkerMetricsCollection()
        worker_metrics.update("worker1", "d", "connected_peers", 20)
        worker_metrics.update("worker2", "d", "connected_peers", 10)
        worker_metrics.update("worker3", "d", "connected_peers", 5)
        worker_metrics.set_online("worker1")
        worker_metrics.set_online("worker2")
        worker_metrics.set_online("worker3")

        # Request MUC location, should get worker2 (least loaded in MUC)
        (worker, _, _) = worker_metrics.get_best_worker("d", location="MUC")
        self.assertEqual(worker, "worker2")


if __name__ == "__main__":
    unittest.main()
