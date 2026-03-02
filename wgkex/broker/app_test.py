"""Integration tests for blacklist feature in broker app."""

import os
import tempfile
import unittest

from wgkex.broker.blacklist import Blacklist


class TestBlacklistValidation(unittest.TestCase):
    """Test blacklist validation logic."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.blacklist_file = os.path.join(self.temp_dir, "blacklist.yaml")

    def tearDown(self):
        """Clean up test fixtures."""
        if os.path.exists(self.blacklist_file):
            os.remove(self.blacklist_file)
        os.rmdir(self.temp_dir)

    def test_check_key_blacklisted_with_reason(self):
        """Test checking a blacklisted key with a reason."""
        blacklist_content = """
- o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg=:
    reason: "Abuse"
"""
        with open(self.blacklist_file, "w") as f:
            f.write(blacklist_content)

        blacklist = Blacklist(self.blacklist_file, auto_reload=False)

        self.assertTrue(
            blacklist.is_blacklisted("o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg=")
        )
        self.assertEqual(
            blacklist.get_reason("o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg="),
            "Abuse",
        )

    def test_check_key_blacklisted_without_reason(self):
        """Test checking a blacklisted key without a reason."""
        blacklist_content = """
- o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg=
"""
        with open(self.blacklist_file, "w") as f:
            f.write(blacklist_content)

        blacklist = Blacklist(self.blacklist_file, auto_reload=False)

        self.assertTrue(
            blacklist.is_blacklisted("o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg=")
        )
        self.assertIsNone(
            blacklist.get_reason("o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg=")
        )

    def test_check_key_not_blacklisted(self):
        """Test checking a key that is not blacklisted."""
        blacklist_content = """
- AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
"""
        with open(self.blacklist_file, "w") as f:
            f.write(blacklist_content)

        blacklist = Blacklist(self.blacklist_file, auto_reload=False)

        self.assertFalse(
            blacklist.is_blacklisted("o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg=")
        )

    def test_empty_blacklist(self):
        """Test checking against an empty blacklist."""
        blacklist_content = ""
        with open(self.blacklist_file, "w") as f:
            f.write(blacklist_content)

        blacklist = Blacklist(self.blacklist_file, auto_reload=False)

        self.assertFalse(
            blacklist.is_blacklisted("o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg=")
        )

    def test_multiple_keys_blacklisted(self):
        """Test checking multiple blacklisted keys."""
        blacklist_content = """
- o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg=:
    reason: "Abuse"
- TszFS3oFRdhsJP3K0VOlklGMGYZy+oFCtlaghXJqW2g=:
    reason: "Spam"
- AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
"""
        with open(self.blacklist_file, "w") as f:
            f.write(blacklist_content)

        blacklist = Blacklist(self.blacklist_file, auto_reload=False)

        self.assertTrue(
            blacklist.is_blacklisted("o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg=")
        )
        self.assertEqual(
            blacklist.get_reason("o52Ge+Rpj4CUSitVag9mS7pSXUesNM0ESnvj/wwehkg="),
            "Abuse",
        )

        self.assertTrue(
            blacklist.is_blacklisted("TszFS3oFRdhsJP3K0VOlklGMGYZy+oFCtlaghXJqW2g=")
        )
        self.assertEqual(
            blacklist.get_reason("TszFS3oFRdhsJP3K0VOlklGMGYZy+oFCtlaghXJqW2g="),
            "Spam",
        )

        self.assertTrue(
            blacklist.is_blacklisted("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        )
        self.assertIsNone(
            blacklist.get_reason("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        )

        self.assertFalse(
            blacklist.is_blacklisted("BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=")
        )


if __name__ == "__main__":
    unittest.main()
