"""Unit tests for app.py"""
import unittest
import mock
import app


class AppTest(unittest.TestCase):
    @mock.patch.object(app.config, "load_config")
    @mock.patch.object(app.mqtt, "connect", autospec=True)
    def test_main_success(self, connect_mock, config_mock):
        """Ensure we can execute main."""
        connect_mock.return_value = None
        config_mock.return_value = dict(domains=["domain.one"])
        app.main()
        connect_mock.assert_called_with(["domain.one"])

    @mock.patch.object(app.config, "load_config")
    @mock.patch.object(app.mqtt, "connect", autospec=True)
    def test_main_fails_no_domain(self, connect_mock, config_mock):
        """Ensure we fail when domains are not configured."""
        connect_mock.return_value = None
        config_mock.return_value = dict(domains=None)
        with self.assertRaises(app.DomainsNotInConfig):
            app.main()


if __name__ == "__main__":
    unittest.main()
