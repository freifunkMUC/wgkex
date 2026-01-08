"""Tests for blacklist module."""

import os
import tempfile
import time
import unittest

from wgkex.broker.blacklist import Blacklist, BlacklistEntry


class TestBlacklist(unittest.TestCase):
    """Test the Blacklist class."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.blacklist_file = os.path.join(self.temp_dir, "blacklist.yaml")

    def tearDown(self):
        """Clean up test fixtures."""
        if os.path.exists(self.blacklist_file):
            os.remove(self.blacklist_file)
        os.rmdir(self.temp_dir)

    def test_empty_blacklist_file_not_exist(self):
        """Test that a non-existent blacklist file results in an empty blacklist."""
        blacklist = Blacklist(self.blacklist_file, auto_reload=False)
        self.assertFalse(blacklist.is_blacklisted("test_key"))

    def test_simple_string_format(self):
        """Test loading blacklist with simple string format."""
        content = """
- Key1
- Key2
- Key3
"""
        with open(self.blacklist_file, "w") as f:
            f.write(content)

        blacklist = Blacklist(self.blacklist_file, auto_reload=False)
        self.assertTrue(blacklist.is_blacklisted("Key1"))
        self.assertTrue(blacklist.is_blacklisted("Key2"))
        self.assertTrue(blacklist.is_blacklisted("Key3"))
        self.assertFalse(blacklist.is_blacklisted("Key4"))
        self.assertIsNone(blacklist.get_reason("Key1"))

    def test_dict_format_with_reason(self):
        """Test loading blacklist with dict format including reasons."""
        content = """
- Key1:
    reason: "Abuse"
- Key2:
    reason: "Spam"
"""
        with open(self.blacklist_file, "w") as f:
            f.write(content)

        blacklist = Blacklist(self.blacklist_file, auto_reload=False)
        self.assertTrue(blacklist.is_blacklisted("Key1"))
        self.assertEqual(blacklist.get_reason("Key1"), "Abuse")
        self.assertTrue(blacklist.is_blacklisted("Key2"))
        self.assertEqual(blacklist.get_reason("Key2"), "Spam")

    def test_dict_format_without_reason(self):
        """Test loading blacklist with dict format without reasons."""
        content = """
- Key1: {}
- Key2: null
"""
        with open(self.blacklist_file, "w") as f:
            f.write(content)

        blacklist = Blacklist(self.blacklist_file, auto_reload=False)
        self.assertTrue(blacklist.is_blacklisted("Key1"))
        self.assertIsNone(blacklist.get_reason("Key1"))
        self.assertTrue(blacklist.is_blacklisted("Key2"))
        self.assertIsNone(blacklist.get_reason("Key2"))

    def test_mixed_format(self):
        """Test loading blacklist with mixed formats."""
        content = """
- Key1
- Key2:
    reason: "Abuse"
- Key3
"""
        with open(self.blacklist_file, "w") as f:
            f.write(content)

        blacklist = Blacklist(self.blacklist_file, auto_reload=False)
        self.assertTrue(blacklist.is_blacklisted("Key1"))
        self.assertIsNone(blacklist.get_reason("Key1"))
        self.assertTrue(blacklist.is_blacklisted("Key2"))
        self.assertEqual(blacklist.get_reason("Key2"), "Abuse")
        self.assertTrue(blacklist.is_blacklisted("Key3"))
        self.assertIsNone(blacklist.get_reason("Key3"))

    def test_empty_file(self):
        """Test loading an empty blacklist file."""
        with open(self.blacklist_file, "w") as f:
            f.write("")

        blacklist = Blacklist(self.blacklist_file, auto_reload=False)
        self.assertFalse(blacklist.is_blacklisted("any_key"))

    def test_invalid_yaml(self):
        """Test that invalid YAML doesn't crash but logs error."""
        content = """
- Key1
  invalid yaml: [
"""
        with open(self.blacklist_file, "w") as f:
            f.write(content)

        blacklist = Blacklist(self.blacklist_file, auto_reload=False)
        # Should have empty blacklist due to parse error
        self.assertFalse(blacklist.is_blacklisted("Key1"))

    def test_manual_reload(self):
        """Test manual reload of blacklist."""
        content = """
- Key1
"""
        with open(self.blacklist_file, "w") as f:
            f.write(content)

        blacklist = Blacklist(self.blacklist_file, auto_reload=False)
        self.assertTrue(blacklist.is_blacklisted("Key1"))
        self.assertFalse(blacklist.is_blacklisted("Key2"))

        # Update file
        content = """
- Key2
"""
        with open(self.blacklist_file, "w") as f:
            f.write(content)

        # Should still see old data
        self.assertTrue(blacklist.is_blacklisted("Key1"))
        self.assertFalse(blacklist.is_blacklisted("Key2"))

        # Reload
        blacklist.reload()

        # Should see new data
        self.assertFalse(blacklist.is_blacklisted("Key1"))
        self.assertTrue(blacklist.is_blacklisted("Key2"))

    def test_auto_reload(self):
        """Test automatic reload when file changes."""
        content = """
- Key1
"""
        with open(self.blacklist_file, "w") as f:
            f.write(content)

        blacklist = Blacklist(self.blacklist_file, auto_reload=True)
        self.assertTrue(blacklist.is_blacklisted("Key1"))
        self.assertFalse(blacklist.is_blacklisted("Key2"))

        # Wait a bit to ensure mtime will be different
        time.sleep(1.1)

        # Update file
        content = """
- Key2
"""
        with open(self.blacklist_file, "w") as f:
            f.write(content)

        # Wait for auto-reload (check interval is 10s but we'll force it faster by polling)
        max_wait = 15  # seconds
        start = time.time()
        while time.time() - start < max_wait:
            if not blacklist.is_blacklisted("Key1") and blacklist.is_blacklisted(
                "Key2"
            ):
                break
            time.sleep(0.5)

        # Should see new data
        self.assertFalse(blacklist.is_blacklisted("Key1"))
        self.assertTrue(blacklist.is_blacklisted("Key2"))

        # Clean up
        blacklist.stop()

    def test_file_deletion(self):
        """Test handling of blacklist file deletion."""
        content = """
- Key1
"""
        with open(self.blacklist_file, "w") as f:
            f.write(content)

        blacklist = Blacklist(self.blacklist_file, auto_reload=False)
        self.assertTrue(blacklist.is_blacklisted("Key1"))

        # Delete file
        os.remove(self.blacklist_file)

        # Reload
        blacklist.reload()

        # Should have empty blacklist
        self.assertFalse(blacklist.is_blacklisted("Key1"))

    def test_reason_string_value(self):
        """Test loading blacklist where reason is a direct string value."""
        content = """
- Key1: "Direct reason string"
"""
        with open(self.blacklist_file, "w") as f:
            f.write(content)

        blacklist = Blacklist(self.blacklist_file, auto_reload=False)
        self.assertTrue(blacklist.is_blacklisted("Key1"))
        self.assertEqual(blacklist.get_reason("Key1"), "Direct reason string")

    def test_dict_top_level_format(self):
        """Test loading blacklist as a top-level dict."""
        content = """
Key1:
  reason: "Abuse"
Key2:
  reason: "Spam"
"""
        with open(self.blacklist_file, "w") as f:
            f.write(content)

        blacklist = Blacklist(self.blacklist_file, auto_reload=False)
        self.assertTrue(blacklist.is_blacklisted("Key1"))
        self.assertEqual(blacklist.get_reason("Key1"), "Abuse")
        self.assertTrue(blacklist.is_blacklisted("Key2"))
        self.assertEqual(blacklist.get_reason("Key2"), "Spam")


if __name__ == "__main__":
    unittest.main()
