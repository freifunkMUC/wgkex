"""Unit tests for app.py"""

import threading
import unittest
from time import sleep
from unittest import mock

from wgkex.config import config
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

    @mock.patch.object(app.signal, "signal")
    @mock.patch.object(app.config, "get_config")
    @mock.patch.object(app.mqtt, "connect", autospec=True)
    def test_main_handles_sigterm(self, connect_mock, config_mock, signal_mock):
        """SIGTERM (docker stop) shuts down gracefully like SIGINT."""
        config_mock.return_value = _get_config_mock()
        with mock.patch.object(app, "flush_workers", return_value=None):
            app.main()
        handlers = {c.args[0]: c.args[1] for c in signal_mock.call_args_list}
        self.assertIn(app.signal.SIGTERM, handlers)
        self.assertIs(handlers[app.signal.SIGTERM], handlers[app.signal.SIGINT])

    @mock.patch.object(app.observability, "start_http_server")
    @mock.patch.object(app.config, "get_config")
    def test_metrics_server_opt_in(self, config_mock, start_mock):
        """The metrics listener is only started if a port is configured."""
        config_mock.return_value = config.Config.from_dict(
            {
                "domains": ["ffmuc_a"],
                "domain_prefixes": ["ffmuc_"],
                "mqtt": {"broker_url": "", "username": "", "password": ""},
            }
        )
        app.start_metrics_server()
        start_mock.assert_not_called()
        config_mock.return_value = config.Config.from_dict(
            {
                "domains": ["ffmuc_a"],
                "domain_prefixes": ["ffmuc_"],
                "mqtt": {"broker_url": "", "username": "", "password": ""},
                "metrics": {"worker_port": 9101, "worker_host": "::"},
            }
        )
        app.start_metrics_server()
        start_mock.assert_called_once_with(9101, "::")

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

    @mock.patch.object(app.config, "get_config")
    @mock.patch.object(app, "wg_flush_stale_peers")
    def test_flush_workers_doesnt_throw(self, wg_flush_mock, config_mock):
        """Ensure the flush_workers thread doesn't throw and exit if it encounters an exception."""
        config_mock.return_value.peer_cleanup = app.config.PeerCleanup(interval=0)
        wg_flush_mock.side_effect = AttributeError(
            "'NoneType' object has no attribute 'get'"
        )

        thread = threading.Thread(
            target=app.flush_workers, args=("dummy_domain",), daemon=True
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

    @mock.patch.object(app.config, "get_config")
    @mock.patch.object(app, "wg_flush_stale_peers")
    def test_flush_workers_keeps_recently_added_peers(self, wg_flush_mock, config_mock):
        """Peers recently added by the key exchange are not removed as stale."""
        config_mock.return_value.peer_cleanup = app.config.PeerCleanup(
            stale_timeout=600, interval=0
        )
        done = threading.Event()

        def flush(domain, timeout, keep):
            self.assertEqual(domain, "cleanup_domain")
            self.assertEqual(timeout, 600)
            self.assertTrue(keep("RECENT_KEY"))
            self.assertFalse(keep("OTHER_KEY"))
            done.set()
            return []

        wg_flush_mock.side_effect = flush
        app.peer_add_tracker.note_added("wg-cleanup_domain", "RECENT_KEY")
        threading.Thread(
            target=app.flush_workers, args=("cleanup_domain",), daemon=True
        ).start()
        self.assertTrue(done.wait(2))

    @mock.patch.object(app.config, "get_config")
    @mock.patch.object(app, "is_allowlisted", side_effect=lambda k: k == "ALLOWED")
    @mock.patch.object(app, "wg_flush_stale_peers")
    def test_flush_workers_keeps_allowlisted_peers(
        self, wg_flush_mock, allowlist_mock, config_mock
    ):
        config_mock.return_value.peer_cleanup = app.config.PeerCleanup(
            stale_timeout=600, interval=0
        )
        done = threading.Event()

        def flush(domain, timeout, keep):
            self.assertTrue(keep("ALLOWED"))
            self.assertFalse(keep("OTHER_KEY"))
            done.set()
            return []

        wg_flush_mock.side_effect = flush
        threading.Thread(
            target=app.flush_workers, args=("allow_domain",), daemon=True
        ).start()
        self.assertTrue(done.wait(2))


if __name__ == "__main__":
    unittest.main()
