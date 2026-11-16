"""Tests for keylist.py"""

import os
import tempfile
import unittest
from unittest import mock

from wgkex.common.keylist import KeyList, parse_keys


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class KeyListTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "keys")
        self.clock = FakeClock()
        self.changes = []

    def write(self, text: str, mtime: float) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)
        os.utime(self.path, (mtime, mtime))

    def keylist(self, **kwargs) -> KeyList:
        return KeyList(
            "test list",
            file=self.path,
            check_interval=60,
            on_change=lambda a, r: self.changes.append((a, r)),
            clock=self.clock,
            **kwargs,
        )

    def test_parse_keys(self):
        self.assertSetEqual(
            parse_keys("a\n\n  b  # comment\n# only comment\nc#x\n"), {"a", "b", "c"}
        )

    def test_unconfigured(self):
        kl = KeyList("empty")
        self.assertFalse(kl.configured)
        self.assertNotIn("a", kl)

    def test_static_keys(self):
        kl = KeyList("static", keys=["a", " b ", ""])
        self.assertTrue(kl.configured)
        self.assertIn("b", kl)
        self.assertSetEqual(kl.keys(), {"a", "b"})

    def test_reload_on_change(self):
        """The file is re-read after check_interval if it was modified."""
        self.write("a\nb\n", 1)
        kl = self.keylist(keys=["s"])
        self.assertSetEqual(kl.keys(), {"a", "b", "s"})
        # The initial load does not notify
        self.assertListEqual(self.changes, [])

        self.write("b\nc\ns\n", 2)
        self.assertIn("a", kl)  # not checked again yet
        self.clock.now += 60
        self.assertNotIn("a", kl)
        self.assertIn("c", kl)
        self.assertListEqual(self.changes, [({"c"}, {"a"})])

    def test_unchanged_file_not_reread(self):
        self.write("a\n", 1)
        kl = self.keylist()
        self.clock.now += 60
        with mock.patch("builtins.open") as open_mock:
            kl.reload()
        open_mock.assert_not_called()

    def test_unreadable_file_keeps_keys(self):
        """Keys stay loaded when the file vanishes, and are reloaded once it is back."""
        self.write("a\n", 1)
        kl = self.keylist()
        os.remove(self.path)
        self.clock.now += 60
        self.assertIn("a", kl)
        self.write("b\n", 1)
        self.clock.now += 60
        self.assertSetEqual(kl.keys(), {"b"})

    def test_missing_file_at_start(self):
        kl = self.keylist()
        self.assertTrue(kl.configured)
        self.assertNotIn("a", kl)
        self.write("a\n", 1)
        self.clock.now += 60
        self.assertIn("a", kl)
        self.assertListEqual(self.changes, [({"a"}, set())])


if __name__ == "__main__":
    unittest.main()
