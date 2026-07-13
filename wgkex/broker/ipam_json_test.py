import ipaddress
import json
import multiprocessing
import os
import tempfile
import unittest
from typing import Any
from unittest import mock

from wgkex.broker.ipam_json import JSONFileIPAM


def _allocate_prefix(
    storage_path: str,
    pubkey: str,
    start: Any,
    results: Any,
) -> None:
    start.wait()
    _, prefix, _ = JSONFileIPAM(storage_path).get_or_allocate_prefix(
        pubkey, ipv4=False, ipv6=True, ipv6_prefix_length=63
    )
    results.put(str(prefix))


class TestJSONFileIPAM(unittest.TestCase):
    def test_atomic_storage_persists_and_reuses_allocations(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            storage_path = os.path.join(temporary_dir, "ipv6_ranges.json")
            ipam = JSONFileIPAM(storage_path)

            _, first_prefix, _ = ipam.get_or_allocate_prefix(
                "pubkey-a", ipv4=False, ipv6=True, ipv6_prefix_length=63
            )
            _, reused_prefix, _ = ipam.get_or_allocate_prefix(
                "pubkey-a", ipv4=False, ipv6=True, ipv6_prefix_length=63
            )
            _, second_prefix, _ = ipam.get_or_allocate_prefix(
                "pubkey-b", ipv4=False, ipv6=True, ipv6_prefix_length=63
            )

            self.assertEqual(first_prefix, reused_prefix)
            self.assertNotEqual(first_prefix, second_prefix)
            with open(storage_path, "r", encoding="utf-8") as ranges_file:
                persisted = json.load(ranges_file)
            self.assertEqual(
                persisted["ranges"],
                {
                    "pubkey-a": str(first_prefix),
                    "pubkey-b": str(second_prefix),
                },
            )

    def test_invalid_json_is_reported_without_overwriting_storage(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            storage_path = os.path.join(temporary_dir, "ipv6_ranges.json")
            with open(storage_path, "w", encoding="utf-8") as ranges_file:
                ranges_file.write("{invalid")

            with self.assertRaises(json.JSONDecodeError):
                JSONFileIPAM(storage_path).get_or_allocate_prefix(
                    "pubkey", ipv4=False, ipv6=True, ipv6_prefix_length=63
                )

            with open(storage_path, "r", encoding="utf-8") as ranges_file:
                self.assertEqual(ranges_file.read(), "{invalid")

    def test_allocation_avoids_overlap_with_differently_sized_ranges(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            storage_path = os.path.join(temporary_dir, "ipv6_ranges.json")
            with open(storage_path, "w", encoding="utf-8") as ranges_file:
                json.dump(
                    {
                        "parent_prefix": "2001:db8:ed0::/56",
                        "ranges": {"pubkey-a": "2001:db8:ed0:2::/64"},
                    },
                    ranges_file,
                )

            _, new_prefix, _ = JSONFileIPAM(storage_path).get_or_allocate_prefix(
                "pubkey-b", ipv4=False, ipv6=True, ipv6_prefix_length=63
            )

            self.assertIsNotNone(new_prefix)
            self.assertFalse(
                new_prefix.overlaps(ipaddress.IPv6Network("2001:db8:ed0:2::/64"))
            )

    def test_exhausted_parent_prefix_returns_no_allocation(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            storage_path = os.path.join(temporary_dir, "ipv6_ranges.json")
            with open(storage_path, "w", encoding="utf-8") as ranges_file:
                json.dump(
                    {
                        "parent_prefix": "2001:db8::/126",
                        "ranges": {"existing": "2001:db8::2/127"},
                    },
                    ranges_file,
                )

            _, prefix, _ = JSONFileIPAM(storage_path).get_or_allocate_prefix(
                "new-pubkey", ipv4=False, ipv6=True, ipv6_prefix_length=127
            )

            self.assertIsNone(prefix)
            with open(storage_path, "r", encoding="utf-8") as ranges_file:
                self.assertEqual(
                    json.load(ranges_file)["ranges"],
                    {"existing": "2001:db8::2/127"},
                )

    def test_atomic_replace_failure_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            storage_path = os.path.join(temporary_dir, "ipv6_ranges.json")
            with (
                mock.patch(
                    "wgkex.broker.ipam_json.os.replace",
                    side_effect=OSError("replace failed"),
                ),
                self.assertRaisesRegex(OSError, "replace failed"),
            ):
                JSONFileIPAM(storage_path).get_or_allocate_prefix(
                    "pubkey", ipv4=False, ipv6=True, ipv6_prefix_length=63
                )

            self.assertEqual(
                [
                    name
                    for name in os.listdir(temporary_dir)
                    if name.startswith(".ipv6_ranges.")
                ],
                [],
            )

    def test_concurrent_processes_allocate_distinct_persistent_prefixes(self):
        context = multiprocessing.get_context("fork")
        with tempfile.TemporaryDirectory() as temporary_dir:
            storage_path = os.path.join(temporary_dir, "ipv6_ranges.json")
            with open(storage_path, "w", encoding="utf-8") as ranges_file:
                json.dump(
                    {"parent_prefix": "2001:db8:ed0::/56", "ranges": {}},
                    ranges_file,
                )

            start = context.Event()
            results = context.Queue()
            processes = [
                context.Process(
                    target=_allocate_prefix,
                    args=(storage_path, f"pubkey-{index}", start, results),
                )
                for index in range(8)
            ]
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)

            allocated = [results.get(timeout=1) for _ in processes]
            self.assertEqual(len(set(allocated)), len(processes))

            with open(storage_path, "r", encoding="utf-8") as ranges_file:
                persisted = json.load(ranges_file)
            self.assertEqual(len(persisted["ranges"]), len(processes))
            self.assertEqual(set(persisted["ranges"].values()), set(allocated))


if __name__ == "__main__":
    unittest.main()
