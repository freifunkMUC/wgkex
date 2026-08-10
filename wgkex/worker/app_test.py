"""Unit tests for the worker application lifecycle."""

import threading
import unittest

import mock

from wgkex.config import config
from wgkex.worker import app


def _get_config_mock(domains=None, parker_enabled=False):
    test_prefixes = ["_TEST_PREFIX_", "_TEST_PREFIX2_"]
    worker_config = mock.MagicMock()
    worker_config.parker.enabled = parker_enabled
    worker_config.parker.prefixes.ipv6.length = 63
    worker_config.cleanup = config.Cleanup(interval=0.01)
    worker_config.domains = (
        domains if domains is not None else [f"{test_prefixes[1]}domain.one"]
    )
    worker_config.domain_prefixes = test_prefixes
    return worker_config


class AppTest(unittest.TestCase):
    def test_unique_domains_success(self):
        self.assertTrue(
            app.check_all_domains_unique(
                ["TEST_PREFIX_ONE", "TEST_PREFIX2_TWO"],
                ["TEST_PREFIX_", "TEST_PREFIX2_"],
            )
        )

    def test_unique_domains_fail(self):
        self.assertFalse(
            app.check_all_domains_unique(
                ["TEST_PREFIX_SAME", "TEST_PREFIX2_SAME"],
                ["TEST_PREFIX_", "TEST_PREFIX2_"],
            )
        )

    def test_unique_domains_validates_prefixes(self):
        with self.assertRaises(TypeError):
            app.check_all_domains_unique(["TEST_PREFIX_ONE"], "TEST_PREFIX_")
        with self.assertRaises(app.PrefixesNotInConfig):
            app.check_all_domains_unique(["TEST_PREFIX_ONE"], [])

    @mock.patch.object(app, "wg_flush_stale_peers")
    def test_flush_workers_uses_configured_timing_and_stops(self, flush):
        flush.return_value = [
            mock.Mock(status="deleted"),
            mock.Mock(status="deferred"),
            mock.Mock(status="failed"),
        ]
        exit_event = mock.MagicMock()
        exit_event.wait.side_effect = [False, True]
        cleanup = config.Cleanup(
            interval=12,
            parker_stale_timeout=345,
            initial_handshake_grace=678,
        )

        app.flush_workers(exit_event, True, "parker", cleanup, 62)

        self.assertEqual(exit_event.wait.call_args_list, [mock.call(12), mock.call(12)])
        flush.assert_called_once_with(
            True,
            "parker",
            stale_timeout=345,
            initial_handshake_grace=678,
            parker_prefix_length=62,
            coordinator=mock.ANY,
        )

    @mock.patch.object(
        app, "wg_flush_stale_peers", side_effect=[RuntimeError("dump failed"), []]
    )
    def test_flush_workers_retries_next_sweep(self, flush):
        exit_event = mock.MagicMock()
        exit_event.wait.side_effect = [False, False, True]

        app.flush_workers(exit_event, False, "domain", config.Cleanup(interval=1))

        self.assertEqual(flush.call_count, 2)

    def test_flush_workers_shutdown_interrupts_wait(self):
        exit_event = threading.Event()
        thread = threading.Thread(
            target=app.flush_workers,
            args=(
                exit_event,
                False,
                "domain",
                config.Cleanup(interval=3600),
            ),
        )
        thread.start()
        exit_event.set()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())

    @mock.patch.object(app.threading, "Thread")
    def test_cleanup_schedules_parker_and_legacy_domains(self, thread):
        parker_config = _get_config_mock(parker_enabled=True)
        exit_event = threading.Event()
        threads = app.clean_up_worker(True, exit_event, parker_config)
        self.assertEqual(threads, [thread.return_value])
        self.assertEqual(thread.call_args.kwargs["name"], "peer-cleanup-parker")
        thread.return_value.start.assert_called_once()

        thread.reset_mock()
        legacy_config = _get_config_mock(
            domains=["_TEST_PREFIX_domain.one", "unmatched"]
        )
        with mock.patch.object(app.logger, "error") as error:
            threads = app.clean_up_worker(False, exit_event, legacy_config)
        self.assertEqual(threads, [thread.return_value])
        self.assertEqual(thread.call_args.kwargs["name"], "peer-cleanup-domain.one")
        thread.return_value.start.assert_called_once()
        error.assert_called_once_with(
            "Not every domain got cleaned. Check domains %s for missing prefixes %s",
            repr(["_TEST_PREFIX_domain.one", "unmatched"]),
            repr(["_TEST_PREFIX_", "_TEST_PREFIX2_"]),
        )

    @mock.patch.object(app.config, "get_config")
    @mock.patch.object(app.mqtt, "connect")
    @mock.patch.object(app, "watch_queue")
    @mock.patch.object(app, "clean_up_worker")
    def test_main_starts_and_joins_worker_threads(
        self, cleanup, watch_queue, connect, get_config
    ):
        worker_config = _get_config_mock(parker_enabled=True)
        get_config.return_value = worker_config
        cleanup_thread = mock.Mock()
        queue_thread = mock.Mock()
        cleanup.return_value = [cleanup_thread]
        watch_queue.return_value = queue_thread

        app.main()

        exit_event = cleanup.call_args.args[1]
        cleanup.assert_called_once_with(True, exit_event, worker_config)
        watch_queue.assert_called_once_with(
            True,
            exit_event,
            parker_prefix_length=63,
            initial_handshake_grace=600,
        )
        connect.assert_called_once_with(exit_event)
        self.assertTrue(exit_event.is_set())
        queue_thread.join.assert_called_once()
        cleanup_thread.join.assert_called_once()

    @mock.patch.object(app.config, "get_config")
    @mock.patch.object(app.mqtt, "connect")
    def test_main_rejects_invalid_legacy_configuration(self, connect, get_config):
        get_config.return_value = _get_config_mock(domains=[])
        with self.assertRaises(app.DomainsNotInConfig):
            app.main()
        connect.assert_not_called()

        get_config.return_value = _get_config_mock(
            domains=["_TEST_PREFIX_same", "_TEST_PREFIX2_same"]
        )
        with self.assertRaises(app.DomainsAreNotUnique):
            app.main()

        get_config.return_value = _get_config_mock(domains=["cant_split_domain"])
        with self.assertRaises(app.InvalidDomain):
            app.main()

    @mock.patch.object(app.config, "get_config")
    def test_signal_sets_exit_event(self, get_config):
        get_config.return_value = _get_config_mock(parker_enabled=True)
        callbacks = []
        with (
            mock.patch.object(
                app.signal,
                "signal",
                side_effect=lambda _, callback: callbacks.append(callback),
            ),
            mock.patch.object(app, "clean_up_worker", return_value=[]),
            mock.patch.object(app, "watch_queue", return_value=mock.Mock()),
            mock.patch.object(app.mqtt, "connect"),
            mock.patch.object(app.sys, "exit") as exit_process,
        ):
            app.main()
            callbacks[0](app.signal.SIGINT, None)

        exit_process.assert_called_once()


if __name__ == "__main__":
    unittest.main()
