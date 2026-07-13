"""Unit tests for app.py"""

import threading
import unittest
from time import sleep

import mock

from wgkex.worker import app


def _get_config_mock(domains=None, parker=None):
    test_prefixes = ["_TEST_PREFIX_", "_TEST_PREFIX2_"]
    config_mock = mock.MagicMock()
    if parker:
        config_mock.parker = parker
    else:
        config_mock.parker.enabled = False
    config_mock.domains = (
        domains if domains is not None else [f"{test_prefixes[1]}domain.one"]
    )
    config_mock.domain_prefixes = test_prefixes
    return config_mock


class AppTest(unittest.TestCase):
    """unittest.TestCase class"""

    def setUp(self) -> None:
        """set up unittests"""
        app._CLEANUP_TIME = 0

    def test_unique_domains_success(self):
        """Ensure domain suffixes are unique."""
        test_prefixes = ["TEST_PREFIX_", "TEST_PREFIX2_"]
        test_domains = [
            "TEST_PREFIX_DOMAINSUFFIX1",
            "TEST_PREFIX_DOMAINSUFFIX2",
            "TEST_PREFIX2_DOMAINSUFFIX3",
        ]
        self.assertTrue(
            app.check_all_domains_unique(test_domains, test_prefixes),
            "unique domains are not detected unique",
        )

    def test_unique_domains_fail(self):
        """Ensure domain suffixes are not unique."""
        test_prefixes = ["TEST_PREFIX_", "TEST_PREFIX2_"]
        test_domains = [
            "TEST_PREFIX_DOMAINSUFFIX1",
            "TEST_PREFIX_DOMAINSUFFIX2",
            "TEST_PREFIX2_DOMAINSUFFIX1",
        ]
        self.assertFalse(
            app.check_all_domains_unique(test_domains, test_prefixes),
            "non-unique domains are detected as unique",
        )

    def test_unique_domains_not_list(self):
        """Ensure domain prefixes are a list."""
        test_prefixes = "TEST_PREFIX_, TEST_PREFIX2_"
        test_domains = [
            "TEST_PREFIX_DOMAINSUFFIX1",
            "TEST_PREFIX_DOMAINSUFFIX2",
            "TEST_PREFIX2_DOMAINSUFFIX1",
        ]
        with self.assertRaises(TypeError):
            app.check_all_domains_unique(test_domains, test_prefixes)

    @mock.patch.object(app.config, "get_config")
    @mock.patch.object(app.mqtt, "connect", autospec=True)
    def test_main_success(self, connect_mock, config_mock):
        """Ensure we can execute main."""
        connect_mock.return_value = None
        config_mock.return_value = _get_config_mock()
        with mock.patch.object(app, "flush_workers", return_value=None):
            app.main()
            connect_mock.assert_called()

    @mock.patch.object(app.config, "get_config")
    @mock.patch.object(app.mqtt, "connect", autospec=True)
    def test_main_fails_no_domain(self, connect_mock, config_mock):
        """Ensure we fail when domains are not configured."""
        config_mock.return_value = _get_config_mock(domains=[])
        connect_mock.return_value = None
        with self.assertRaises(app.DomainsNotInConfig):
            app.main()

    @mock.patch.object(app.config, "get_config")
    @mock.patch.object(app.mqtt, "connect", autospec=True)
    def test_main_fails_bad_domain(self, connect_mock, config_mock):
        """Ensure we fail when domains are badly formatted."""
        config_mock.return_value = _get_config_mock(domains=["cant_split_domain"])
        connect_mock.return_value = None
        with self.assertRaises(app.InvalidDomain):
            app.main()
        connect_mock.assert_not_called()

    @mock.patch.object(app, "_CLEANUP_TIME", 1)
    @mock.patch.object(app, "wg_flush_stale_peers")
    def test_flush_workers_doesnt_throw(self, wg_flush_mock):
        """Ensure the flush_workers thread doesn't throw and exit if it encounters an exception."""
        wg_flush_mock.side_effect = AttributeError(
            "'NoneType' object has no attribute 'get'"
        )

        thread = threading.Thread(
            target=app.flush_workers, args=(False, "dummy_domain"), daemon=True
        )
        thread.start()

        i = 0
        while i < 20 and not wg_flush_mock.called:
            i += 1
            sleep(0.1)

        wg_flush_mock.assert_called()
        # Assert that the thread hasn't crashed and is still running
        self.assertTrue(thread.is_alive())
        # If Python would allow it without writing custom signalling, this would be the place to stop the thread again

    @mock.patch.object(app.threading, "Thread")
    def test_cleanup_schedules_parker_and_legacy_domains(self, thread):
        app.clean_up_worker(True)
        thread.assert_called_once_with(
            target=app.flush_workers, args=(True, "parker"), daemon=True
        )
        thread.return_value.start.assert_called_once()

        thread.reset_mock()
        with (
            mock.patch.object(
                app.config,
                "get_config",
                return_value=_get_config_mock(
                    domains=["_TEST_PREFIX_domain.one", "unmatched"]
                ),
            ),
            mock.patch.object(app.logger, "error") as error,
        ):
            app.clean_up_worker(False)
        thread.assert_called_once_with(
            target=app.flush_workers, args=(False, "domain.one"), daemon=True
        )
        thread.return_value.start.assert_called_once()
        error.assert_called_once_with(
            "Not every domain got cleaned. Check domains %s for missing prefixes %s",
            repr(["_TEST_PREFIX_domain.one", "unmatched"]),
            repr(["_TEST_PREFIX_", "_TEST_PREFIX2_"]),
        )

    @mock.patch.object(app.config, "get_config")
    def test_main_parker_registers_shutdown_and_starts_components(self, config_mock):
        config_mock.return_value = _get_config_mock(parker=mock.MagicMock(enabled=True))
        callbacks = []
        with (
            mock.patch.object(
                app.signal,
                "signal",
                side_effect=lambda _, callback: callbacks.append(callback),
            ),
            mock.patch.object(app, "clean_up_worker") as cleanup,
            mock.patch.object(app, "watch_queue") as watch_queue,
            mock.patch.object(app.mqtt, "connect") as connect,
        ):
            app.main()

        cleanup.assert_called_once_with(True)
        watch_queue.assert_called_once_with(True)
        connect.assert_called_once()
        self.assertEqual(len(callbacks), 2)

        with (
            mock.patch.object(app.time, "sleep") as sleep,
            mock.patch.object(app.sys, "exit") as exit_process,
        ):
            callbacks[0](app.signal.SIGINT, None)
        sleep.assert_called_once_with(2)
        exit_process.assert_called_once()

    @mock.patch.object(app.config, "get_config")
    def test_main_rejects_duplicate_stripped_domains(self, config_mock):
        config_mock.return_value = _get_config_mock(
            domains=["_TEST_PREFIX_same", "_TEST_PREFIX2_same"]
        )
        with (
            mock.patch.object(app.signal, "signal"),
            self.assertRaises(app.DomainsAreNotUnique),
        ):
            app.main()


if __name__ == "__main__":
    unittest.main()
