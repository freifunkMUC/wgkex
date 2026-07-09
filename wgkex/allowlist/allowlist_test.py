"""Unit tests for allowlist.py"""

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import yaml

from wgkex.allowlist import allowlist


class AllowlistManagerTest(unittest.TestCase):
    """Tests for AllowlistManager class."""

    def setUp(self):
        """Set up test fixtures."""
        self.test_dir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.test_dir, "allowlist.yaml")

    def tearDown(self):
        """Clean up test fixtures."""
        if os.path.exists(self.test_file):
            os.remove(self.test_file)
        os.rmdir(self.test_dir)

    def _create_allowlist_file(self, data: dict) -> None:
        """Helper to create an allowlist YAML file."""
        with open(self.test_file, "w") as f:
            yaml.dump(data, f)

    def test_load_allowlist_success(self):
        """Test successful loading of allowlist file."""
        test_data = {
            "domain1": ["key1", "key2", "key3"],
            "domain2": ["key4", "key5"],
        }
        self._create_allowlist_file(test_data)

        manager = allowlist.AllowlistManager(self.test_file, refresh_interval=0)

        self.assertEqual(set(manager.get_domains()), {"domain1", "domain2"})
        self.assertEqual(
            set(manager.get_allowed_keys("domain1")), {"key1", "key2", "key3"}
        )
        self.assertEqual(set(manager.get_allowed_keys("domain2")), {"key4", "key5"})

    def test_is_key_allowed_success(self):
        """Test key validation against allowlist."""
        test_data = {
            "domain1": ["allowed_key_1", "allowed_key_2"],
            "domain2": ["allowed_key_3"],
        }
        self._create_allowlist_file(test_data)

        manager = allowlist.AllowlistManager(self.test_file, refresh_interval=0)

        self.assertTrue(manager.is_key_allowed("domain1", "allowed_key_1"))
        self.assertTrue(manager.is_key_allowed("domain1", "allowed_key_2"))
        self.assertTrue(manager.is_key_allowed("domain2", "allowed_key_3"))

    def test_is_key_allowed_failure(self):
        """Test key rejection when not in allowlist."""
        test_data = {
            "domain1": ["allowed_key_1"],
        }
        self._create_allowlist_file(test_data)

        manager = allowlist.AllowlistManager(self.test_file, refresh_interval=0)

        self.assertFalse(manager.is_key_allowed("domain1", "not_allowed_key"))
        self.assertFalse(manager.is_key_allowed("domain2", "allowed_key_1"))
        self.assertFalse(manager.is_key_allowed("nonexistent", "any_key"))

    def test_load_empty_file(self):
        """Test loading an empty allowlist file."""
        with open(self.test_file, "w") as f:
            f.write("")

        manager = allowlist.AllowlistManager(self.test_file, refresh_interval=0)

        self.assertEqual(manager.get_domains(), [])
        self.assertFalse(manager.is_key_allowed("any_domain", "any_key"))

    def test_load_missing_file(self):
        """Test handling of missing allowlist file."""
        nonexistent_file = os.path.join(self.test_dir, "nonexistent.yaml")

        manager = allowlist.AllowlistManager(nonexistent_file, refresh_interval=0)

        self.assertEqual(manager.get_domains(), [])
        self.assertFalse(manager.is_key_allowed("any_domain", "any_key"))

    def test_load_invalid_yaml(self):
        """Test handling of malformed YAML file."""
        with open(self.test_file, "w") as f:
            f.write("invalid: yaml: content: [")

        with self.assertRaises(yaml.YAMLError):
            allowlist.AllowlistManager(self.test_file, refresh_interval=0)

    def test_load_invalid_format(self):
        """Test handling of invalid allowlist format."""
        # Domain value should be a list, not a string
        test_data = {"domain1": "not_a_list"}
        self._create_allowlist_file(test_data)

        manager = allowlist.AllowlistManager(self.test_file, refresh_interval=0)

        # Invalid domain should be skipped
        self.assertEqual(manager.get_domains(), [])

    def test_reload(self):
        """Test reloading the allowlist."""
        # Initial data
        test_data = {"domain1": ["key1"]}
        self._create_allowlist_file(test_data)

        manager = allowlist.AllowlistManager(self.test_file, refresh_interval=0)
        self.assertTrue(manager.is_key_allowed("domain1", "key1"))
        self.assertFalse(manager.is_key_allowed("domain1", "key2"))

        # Update file
        test_data = {"domain1": ["key2", "key3"]}
        self._create_allowlist_file(test_data)

        # Reload
        manager.reload()
        self.assertFalse(manager.is_key_allowed("domain1", "key1"))
        self.assertTrue(manager.is_key_allowed("domain1", "key2"))
        self.assertTrue(manager.is_key_allowed("domain1", "key3"))

    def test_periodic_refresh(self):
        """Test periodic refresh mechanism."""
        test_data = {"domain1": ["key1"]}
        self._create_allowlist_file(test_data)

        # Use short refresh interval for testing
        manager = allowlist.AllowlistManager(self.test_file, refresh_interval=1)

        try:
            self.assertTrue(manager.is_key_allowed("domain1", "key1"))

            # Update file
            test_data = {"domain1": ["key2"]}
            self._create_allowlist_file(test_data)

            # Wait for refresh
            time.sleep(1.5)

            # Should reflect new data
            self.assertFalse(manager.is_key_allowed("domain1", "key1"))
            self.assertTrue(manager.is_key_allowed("domain1", "key2"))
        finally:
            manager.stop()

    def test_thread_safety(self):
        """Test thread-safe access to allowlist."""
        test_data = {"domain1": [f"key{i}" for i in range(100)]}
        self._create_allowlist_file(test_data)

        manager = allowlist.AllowlistManager(self.test_file, refresh_interval=0)

        results = []
        errors = []

        def check_keys():
            try:
                for i in range(100):
                    result = manager.is_key_allowed("domain1", f"key{i}")
                    results.append(result)
            except Exception as e:
                errors.append(e)

        # Create multiple threads
        threads = [threading.Thread(target=check_keys) for _ in range(5)]

        # Start all threads
        for t in threads:
            t.start()

        # Wait for completion
        for t in threads:
            t.join()

        # No errors should occur
        self.assertEqual(len(errors), 0)
        # All keys should be found
        self.assertTrue(all(results))

    def test_get_allowed_keys_empty_domain(self):
        """Test getting keys for non-existent domain."""
        test_data = {"domain1": ["key1"]}
        self._create_allowlist_file(test_data)

        manager = allowlist.AllowlistManager(self.test_file, refresh_interval=0)

        self.assertEqual(manager.get_allowed_keys("nonexistent"), [])

    def test_stop_refresh_thread(self):
        """Test stopping the refresh thread."""
        test_data = {"domain1": ["key1"]}
        self._create_allowlist_file(test_data)

        manager = allowlist.AllowlistManager(self.test_file, refresh_interval=1)

        # Verify thread is running
        self.assertIsNotNone(manager._refresh_thread)
        self.assertTrue(manager._refresh_thread.is_alive())

        # Stop the thread
        manager.stop()

        # Wait a bit for thread to stop
        time.sleep(0.5)

        # Thread should be stopped
        self.assertFalse(manager._refresh_thread.is_alive())

    def test_no_refresh_thread_when_interval_zero(self):
        """Test that no refresh thread is created when interval is 0."""
        test_data = {"domain1": ["key1"]}
        self._create_allowlist_file(test_data)

        manager = allowlist.AllowlistManager(self.test_file, refresh_interval=0)

        self.assertIsNone(manager._refresh_thread)


if __name__ == "__main__":
    unittest.main()
