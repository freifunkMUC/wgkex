"""Unit tests for app.py"""
import unittest
import mock
import app


class AppTest(unittest.TestCase):
    def setUp(self) -> None:
        app._CLEANUP_TIME = 0

    @mock.patch.object(app.config, "load_config")
    @mock.patch.object(app.mqtt, "connect", autospec=True)
    def test_main_success(self, connect_mock, config_mock):
        """Ensure we can execute main."""
        connect_mock.return_value = None
        test_prefix = "TEST_PREFIX_"
        config_mock.return_value = dict(
            domains=[f"{test_prefix}domain.one"], domain_prefix=test_prefix
        )
        with mock.patch("app.flush_workers", return_value=None):
            app.main()
            connect_mock.assert_called_with()

    @mock.patch.object(app.config, "load_config")
    @mock.patch.object(app.mqtt, "connect", autospec=True)
    def test_main_fails_no_domain(self, connect_mock, config_mock):
        """Ensure we fail when domains are not configured."""
        config_mock.return_value = dict(domains=None)
        connect_mock.return_value = None
        with self.assertRaises(app.DomainsNotInConfig):
            app.main()

    @mock.patch.object(app.config, "load_config")
    @mock.patch.object(app.mqtt, "connect", autospec=True)
    def test_main_fails_bad_domain(self, connect_mock, config_mock):
        """Ensure we fail when domains are badly formatted."""
        test_prefix = "TEST_PREFIX_"
        config_mock.return_value = dict(
            domains=[f"cant_split_domain"], domain_prefix=test_prefix
        )
        connect_mock.return_value = None
        with mock.patch("app.flush_workers", return_value=None):
            app.main()
            connect_mock.assert_called_with()

    @mock.patch("time.sleep", side_effect=InterruptedError)
    @mock.patch("app.wg_flush_stale_peers")
    def test_flush_workers(self, flush_mock, sleep_mock):
        """Ensure we fail when domains are badly formatted."""
        flush_mock.return_value = ""
        # Infinite loop in flush_workers has no exit value, so test will generate one, and test for that.
        with self.assertRaises(InterruptedError):
            app.flush_workers("test_domain")


if __name__ == "__main__":
    unittest.main()
