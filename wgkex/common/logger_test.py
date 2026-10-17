"""Tests for logger.py"""

import os
import tempfile
import unittest

from wgkex.common import logger


class LoggerTest(unittest.TestCase):
    def fetch(self, content: str) -> dict:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(content)
        try:
            return logger.fetch_logging_configuration(f.name)
        finally:
            os.unlink(f.name)

    def test_default_level_info(self):
        self.assertEqual(self.fetch("domains: []\n")["root"]["level"], "INFO")

    def test_missing_file_uses_default(self):
        cfg = logger.fetch_logging_configuration("/nonexistent/wgkex.yaml")
        self.assertEqual(cfg, logger._LOGGING_DEFAULT_CONFIG)

    def test_log_level_honored(self):
        self.assertEqual(self.fetch("log_level: warning\n")["root"]["level"], "WARNING")
        # The default config must not be modified
        self.assertEqual(logger._LOGGING_DEFAULT_CONFIG["root"]["level"], "INFO")

    def test_log_level_overrides_logging_config(self):
        cfg = self.fetch(
            "log_level: ERROR\nlogging_config:\n  version: 1\n  root:\n    level: DEBUG\n"
        )
        self.assertEqual(cfg["root"]["level"], "ERROR")

    def test_invalid_log_level_ignored(self):
        self.assertEqual(self.fetch("log_level: LOUD\n")["root"]["level"], "INFO")


if __name__ == "__main__":
    unittest.main()
