import json
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from wgkex.broker.ipam_netbox import NetboxIPAM, _TimeoutSession
from wgkex.config import config

_test_config = config.Config.from_dict(
    {
        "parker": {
            "enabled": True,
            "464xlat": True,
            "ipam": "json",
            "prefixes": {
                "ipv4": {"clat_subnet": "10.80.96.0/22"},
                "ipv6": {"length": 63},
            },
        },
        "broker_signing_key": "longstring",
        "domains": [],
        "domain_prefixes": "",
        "workers": {},
        "mqtt": {"broker_url": "", "username": "", "password": ""},
    }
)


def _mocked_netbox_response(*args, **kwargs):
    class MockResponse(mock.MagicMock):
        # def __init__(self,):
        #     super().__init__()  # spec=requests.Response

        @staticmethod
        def with_data(status_code, json_data):
            mr = MockResponse()
            mr.status_code = status_code
            mr.ok = 200 <= status_code < 300
            mr._json_data = json_data
            return mr

        def json(self):
            return self._json_data

    if args[0] == "GET":
        # Parent prefix
        if args[1].endswith("/api/ipam/prefixes/") and kwargs.get("params", {}) == {
            "family": 6,
            "limit": 0,
        }:
            return MockResponse.with_data(
                status_code=200,
                json_data={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [
                        {
                            "id": 1,
                            "prefix": "2001:db8:99::/48",
                            "description": "",
                        }
                    ],
                },
            )
        if args[1].endswith("/api/ipam/prefixes/") and kwargs.get("params", {}) == {
            "family": 6,
            "within": "2001:db8:99::/48",
            "description__ic": "pubkey",
            "limit": 0,
        }:
            return MockResponse.with_data(
                status_code=200,
                json_data={
                    "count": 1,
                    "next": None,
                    "previous": None,
                    "results": [
                        {
                            "id": 2,
                            "prefix": "2001:db8:99::/63",
                            "description": '{"pubkey": "pubkey"}',
                        }
                    ],
                },
            )
    if args[0] == "POST" and args[1].endswith("/api/ipam/prefixes/"):
        return MockResponse.with_data(
            status_code=201,
            json_data={
                "id": 2,
                "prefix": "2001:db8:999::/63",
                "description": '{"pubkey": "pubkey"}',
            },
        )
    if args[0] == "PATCH" and args[1].endswith("/api/ipam/prefixes/2/"):
        return MockResponse.with_data(status_code=200, json_data={})

    print(f"Unexpected request in _mocked_netbox_response: {args}, {kwargs}")
    return MockResponse.with_data(status_code=404, json_data={})


class TestNetboxIPAM(unittest.TestCase):
    ipam: NetboxIPAM

    @classmethod
    def setUpClass(cls):
        config._parsed_config = _test_config

        with mock.patch(
            "requests.sessions.Session.request", side_effect=_mocked_netbox_response
        ):
            cls.ipam = NetboxIPAM("http://localhost:54321", "", True)

    @mock.patch(
        "requests.sessions.Session.request", side_effect=_mocked_netbox_response
    )
    def test_lookup_and_allocate_return_entries(self, _):
        entry = {
            "prefix": "2001:db8:99::/63",
            "concentrators": [],
            "primary": False,
            "written": 0.0,
        }
        self.assertEqual(self.ipam.lookup("pubkey"), entry)
        self.assertEqual(self.ipam.allocate("pubkey", 63), entry)

    def test_requests_have_a_default_timeout(self):
        self.assertIsInstance(self.ipam.nb.http_session, _TimeoutSession)
        with mock.patch("requests.Session.request") as request:
            self.ipam.nb.http_session.request("GET", "http://x")
            self.ipam.nb.http_session.request("GET", "http://x", timeout=1)
        self.assertEqual([c.kwargs["timeout"] for c in request.call_args_list], [5, 1])

    def _store_ipam(self, records):
        ipam = object.__new__(NetboxIPAM)
        ipam.parent_prefix_v6 = mock.MagicMock(prefix="2001:db8::/48")
        ipam.nb = mock.MagicMock()
        ipam.nb.ipam.prefixes.filter.return_value = records
        return ipam

    def test_store_writes_entry_and_returns_canonical_prefix(self):
        record = mock.MagicMock(
            prefix="2001:db8:0:2::/63",
            description='{"reserved_by": "broker1", "created_by": "wgkex"}',
        )
        ipam = self._store_ipam([record])
        canonical = mock.MagicMock(prefix="2001:db8::/63")
        with mock.patch.object(
            ipam, "_deduplicate_prefixes", return_value=canonical
        ) as dedupe:
            self.assertEqual(
                ipam.store("pubkey", "2001:db8:0:2::/63", ["gw1"], True),
                "2001:db8::/63",
            )
        dedupe.assert_called_once_with("pubkey", 6, allocated_prefix=record)
        record.save.assert_called_once()
        desc = json.loads(record.description)
        self.assertEqual(desc["pubkey"], "pubkey")
        self.assertEqual(desc["selected_concentrators"], ["gw1"])
        self.assertTrue(desc["primary"])
        self.assertEqual(desc["last_seen"], desc["last_allocated_on"])
        self.assertNotIn("reserved_by", desc)

    def test_store_rejects_missing_or_foreign_prefix(self):
        with self.assertRaisesRegex(ValueError, "not in NetBox"):
            self._store_ipam([]).store("pubkey", "2001:db8::/63", [], False)
        other = mock.MagicMock(description='{"pubkey": "other"}')
        with self.assertRaisesRegex(ValueError, "another node"):
            self._store_ipam([other]).store("pubkey", "2001:db8::/63", [], False)
        other.save.assert_not_called()

    def test_dump_reads_entries_and_own_reserved_prefixes(self):
        def prefix(net, desc, prefix_id=1):
            return mock.MagicMock(id=prefix_id, prefix=net, description=desc)

        dup_a = prefix("2001:db8:0:8::/63", '{"pubkey": "dup"}', 8)
        records = [
            prefix(
                "2001:db8::/63",
                '{"pubkey": "a", "primary": true, "selected_concentrators": ["gw"],'
                ' "last_allocated_on": "2026-01-01T00:00:00+00:00"}',
            ),
            prefix("2001:db8:0:2::/63", '{"reserved_by": "broker1"}'),
            prefix("2001:db8:0:4::/63", '{"reserved_by": "broker2"}'),
            prefix("2001:db8:0:6::/63", "not json"),
            dup_a,
            prefix("2001:db8:0:a::/63", '{"pubkey": "dup"}', 9),
        ]
        ipam = self._store_ipam(records)
        with mock.patch.object(
            ipam, "_deduplicate_prefixes", return_value=dup_a
        ) as dedupe:
            entries, reserved = ipam.dump("broker1")
        dedupe.assert_called_once_with("dup", 6)
        self.assertEqual(reserved, ["2001:db8:0:2::/63"])
        self.assertEqual(set(entries), {"a", "dup"})
        self.assertEqual(entries["a"]["concentrators"], ["gw"])
        self.assertTrue(entries["a"]["primary"])
        self.assertEqual(entries["a"]["written"], 1767225600.0)
        self.assertEqual(entries["dup"]["prefix"], "2001:db8:0:8::/63")

    def test_seen_mark_seen_and_release_only_touch_wgkex_node_prefixes(self):
        def prefix(net, desc):
            return mock.MagicMock(prefix=net, description=json.dumps(desc))

        seen = prefix(
            "2001:db8::/63",
            {
                "pubkey": "a",
                "created_by": "wgkex",
                "last_seen": "2026-01-01T00:00:00+00:00",
            },
        )
        unmarked = prefix("2001:db8:0:2::/63", {"pubkey": "b", "created_by": "wgkex"})
        manual = prefix("2001:db8:0:4::/63", {"pubkey": "c"})
        reserved = prefix(
            "2001:db8:0:6::/63", {"reserved_by": "broker1", "created_by": "wgkex"}
        )
        ipam = self._store_ipam([seen, unmarked, manual, reserved])
        self.assertEqual(ipam.seen(), {"a": 1767225600.0, "b": None})

        self.assertEqual(ipam.mark_seen({"a", "b", "c"}), 1)
        seen.save.assert_not_called()
        manual.save.assert_not_called()
        desc = json.loads(unmarked.description)
        self.assertEqual(desc["pubkey"], "b")
        self.assertIn("last_seen", desc)

        with mock.patch.object(ipam, "_get_prefixes", return_value=[manual]):
            self.assertEqual(ipam.release("c"), [])
        manual.delete.assert_not_called()
        with mock.patch.object(ipam, "_get_prefixes", return_value=[seen]):
            self.assertEqual(ipam.release("a"), ["2001:db8::/63"])
        seen.delete.assert_called_once()

    def test_reserve_creates_marked_prefixes(self):
        ipam = self._store_ipam([])
        ipam.parent_prefix_v6.available_prefixes.create.return_value = [
            {"prefix": "2001:db8::/63"},
            mock.MagicMock(prefix="2001:db8:0:2::/63"),
        ]
        self.assertEqual(
            ipam.reserve("broker1", 2, 63), ["2001:db8::/63", "2001:db8:0:2::/63"]
        )
        (data,), _ = ipam.parent_prefix_v6.available_prefixes.create.call_args
        self.assertEqual(len(data), 2)
        self.assertEqual(json.loads(data[0]["description"])["reserved_by"], "broker1")
        self.assertTrue(data[0]["mark_utilized"])

    def test_concurrent_same_pubkey_allocations_converge_and_release_duplicate(self):
        class FakePrefix:
            def __init__(self, backend, prefix_id, prefix):
                self.backend = backend
                self.id = prefix_id
                self.prefix = prefix
                self.description = json.dumps(
                    {
                        "pubkey": "race-pubkey",
                        "selected_concentrators": ["worker-a"],
                    }
                )

            def delete(self):
                with self.backend.lock:
                    if self in self.backend.prefixes:
                        self.backend.prefixes.remove(self)
                return True

        class SharedBackend:
            def __init__(self):
                self.lock = threading.Lock()
                self.initial_lookup = threading.Barrier(2)
                self.allocations = threading.Barrier(2)
                self.prefixes = []
                self.lookup_count = 0

            def filter(self, **kwargs):
                with self.lock:
                    self.lookup_count += 1
                    lookup_number = self.lookup_count
                if lookup_number <= 2:
                    self.initial_lookup.wait(timeout=5)
                    return []
                with self.lock:
                    return list(self.prefixes)

            def create(self, data):
                with self.lock:
                    prefix_id = len(self.prefixes) + 10
                    prefix = FakePrefix(
                        self,
                        prefix_id,
                        f"2001:db8:99:{(prefix_id - 10) * 2:x}::/63",
                    )
                    self.prefixes.append(prefix)
                self.allocations.wait(timeout=5)
                return prefix

        backend = SharedBackend()

        def make_ipam():
            ipam = object.__new__(NetboxIPAM)
            ipam.parent_prefix_v6 = mock.MagicMock(prefix="2001:db8:99::/48")
            ipam.parent_prefix_v6.available_prefixes.create = backend.create
            ipam.nb = mock.MagicMock()
            ipam.nb.ipam.prefixes.filter = backend.filter
            return ipam

        def allocate(ipam):
            return ipam._get_or_allocate_prefix("race-pubkey", 6, 63, None)

        with (
            mock.patch(
                "wgkex.broker.ipam_netbox.pynetbox.models.ipam.Prefixes", FakePrefix
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            results = list(executor.map(allocate, [make_ipam(), make_ipam()]))

        self.assertEqual([result[0].id for result in results], [10, 10])
        self.assertEqual(
            [result[1] for result in results],
            [["worker-a"], ["worker-a"]],
        )
        self.assertEqual([prefix.id for prefix in backend.prefixes], [10])

    def test_duplicate_release_failure_does_not_report_convergence(self):
        canonical = mock.MagicMock(
            id=10,
            prefix="2001:db8:99::/63",
            description='{"pubkey": "pubkey"}',
        )
        duplicate = mock.MagicMock(
            id=11,
            prefix="2001:db8:99:2::/63",
            description='{"pubkey": "pubkey"}',
        )
        duplicate.delete.return_value = False

        with (
            mock.patch.object(
                self.ipam,
                "_get_prefixes",
                return_value=[canonical, duplicate],
            ),
            self.assertRaises(RuntimeError),
        ):
            self.ipam._deduplicate_prefixes("pubkey", 6)

    def test_allocated_prefix_is_kept_when_requery_visibility_is_delayed(self):
        allocated = mock.MagicMock(
            id=10,
            prefix="2001:db8:99::/63",
            description='{"pubkey": "pubkey"}',
        )

        with mock.patch.object(self.ipam, "_get_prefixes", return_value=[]):
            canonical = self.ipam._deduplicate_prefixes(
                "pubkey", 6, allocated_prefix=allocated
            )

        self.assertIs(canonical, allocated)
        allocated.delete.assert_not_called()

    def test_concurrent_duplicate_delete_404_is_already_reconciled(self):
        class FakeRequestError(Exception):
            def __init__(self):
                self.req = mock.MagicMock(status_code=404)

        canonical = mock.MagicMock(
            id=10,
            prefix="2001:db8:99::/63",
            description='{"pubkey": "pubkey"}',
        )
        duplicate = mock.MagicMock(
            id=11,
            prefix="2001:db8:99:2::/63",
            description='{"pubkey": "pubkey"}',
        )
        duplicate.delete.side_effect = FakeRequestError()

        with (
            mock.patch.object(
                self.ipam,
                "_get_prefixes",
                return_value=[canonical, duplicate],
            ),
            mock.patch(
                "wgkex.broker.ipam_netbox.pynetbox.core.query.RequestError",
                FakeRequestError,
            ),
        ):
            reconciled = self.ipam._deduplicate_prefixes("pubkey", 6)

        self.assertIs(reconciled, canonical)

    def test_duplicate_delete_server_error_fails_reconciliation(self):
        class FakeRequestError(Exception):
            def __init__(self):
                self.req = mock.MagicMock(status_code=500)

        canonical = mock.MagicMock(id=10, prefix="2001:db8:99::/63")
        duplicate = mock.MagicMock(id=11, prefix="2001:db8:99:2::/63")
        duplicate.delete.side_effect = FakeRequestError()

        with (
            mock.patch.object(
                self.ipam,
                "_get_prefixes",
                return_value=[canonical, duplicate],
            ),
            mock.patch(
                "wgkex.broker.ipam_netbox.pynetbox.core.query.RequestError",
                FakeRequestError,
            ),
            self.assertRaisesRegex(RuntimeError, "Failed to release duplicate prefix"),
        ):
            self.ipam._deduplicate_prefixes("pubkey", 6)

    def test_constructor_validates_parent_prefixes(self):
        def records(*items):
            result = mock.MagicMock()
            result.__len__.return_value = len(items)
            result.__next__.side_effect = iter(items)
            return result

        cfg = mock.MagicMock()
        cfg.parker.prefixes.ipv6.netbox_filter = {}
        cfg.parker.prefixes.ipv4.netbox_filter = None
        nb = mock.MagicMock()

        with (
            mock.patch("wgkex.broker.ipam_netbox.config.get_config", return_value=cfg),
            mock.patch("wgkex.broker.ipam_netbox.pynetbox.api", return_value=nb),
        ):
            nb.ipam.prefixes.filter.return_value = records()
            with self.assertRaisesRegex(ValueError, "parent IPv6 prefix"):
                NetboxIPAM("https://netbox", "token")

            parent_v6 = mock.MagicMock(prefix="2001:db8::/48")
            nb.ipam.prefixes.filter.return_value = records(parent_v6)
            with self.assertRaisesRegex(ValueError, "no IPv4 NetBox filter"):
                NetboxIPAM("https://netbox", "token")

            cfg.parker.prefixes.ipv4.netbox_filter = {"role": "wgkex"}
            nb.ipam.prefixes.filter.side_effect = [
                records(parent_v6),
                records(),
            ]
            with self.assertRaisesRegex(ValueError, "parent IPv4 prefix"):
                NetboxIPAM("https://netbox", "token")

            parent_v4 = mock.MagicMock(prefix="10.0.0.0/8")
            nb.ipam.prefixes.filter.side_effect = [
                records(parent_v6),
                records(parent_v4),
            ]
            ipam = NetboxIPAM("https://netbox", "token")
            self.assertIs(ipam.parent_prefix_v4, parent_v4)

    def test_prefix_lookup_ignores_malformed_descriptions(self):
        ipam = object.__new__(NetboxIPAM)
        ipam.parent_prefix_v6 = mock.MagicMock(prefix="2001:db8::/48")
        ipam.nb = mock.MagicMock()
        invalid_json = mock.MagicMock(prefix="2001:db8::/63", description="{")
        non_object = mock.MagicMock(prefix="2001:db8:0:2::/63", description="[]")
        wrong_key = mock.MagicMock(
            prefix="2001:db8:0:4::/63",
            description='{"pubkey": "someone-else"}',
        )
        match = mock.MagicMock(
            prefix="2001:db8:0:6::/63",
            description='{"pubkey": "pubkey"}',
        )
        ipam.nb.ipam.prefixes.filter.return_value = [
            invalid_json,
            non_object,
            wrong_key,
            match,
        ]
        self.assertEqual(ipam._get_prefixes("pubkey", 6), [match])
        no_id = mock.MagicMock(spec=["prefix"])
        no_id.prefix = "2001:db8::/63"
        self.assertEqual(
            self.ipam._prefix_sort_key(no_id),
            (sys.maxsize, "2001:db8::/63"),
        )

    def test_existing_prefix_invalid_concentrators_are_ignored(self):
        prefix = mock.MagicMock(
            prefix="2001:db8::/63",
            description='{"pubkey":"pubkey","selected_concentrators":"worker"}',
        )
        with mock.patch.object(self.ipam, "_deduplicate_prefixes", return_value=prefix):
            returned, workers = self.ipam._get_or_allocate_prefix("pubkey", 6, 63, None)
        self.assertIs(returned, prefix)
        self.assertEqual(workers, [])

    def test_allocation_failures_return_no_prefix(self):
        class FakeRequestError(Exception):
            pass

        parent = mock.MagicMock()
        ipam = object.__new__(NetboxIPAM)
        ipam.parent_prefix_v6 = parent
        with (
            mock.patch.object(ipam, "_deduplicate_prefixes", return_value=None),
            mock.patch(
                "wgkex.broker.ipam_netbox.pynetbox.core.query.RequestError",
                FakeRequestError,
            ),
        ):
            parent.available_prefixes.create.side_effect = FakeRequestError()
            self.assertEqual(
                ipam._get_or_allocate_prefix("pubkey", 6, 63, None),
                (None, []),
            )

            parent.available_prefixes.create.side_effect = None
            parent.available_prefixes.create.return_value = {"prefix": "invalid"}
            self.assertEqual(
                ipam._get_or_allocate_prefix("pubkey", 6, 63, None),
                (None, []),
            )

    def test_ipv4_lookups_and_allocations_use_ipv4_parent(self):
        ipam = object.__new__(NetboxIPAM)
        ipam.nb = mock.MagicMock()
        ipam.parent_prefix_v4 = mock.MagicMock(prefix="10.0.0.0/8")
        ipam.parent_prefix_v6 = mock.MagicMock(prefix="2001:db8::/32")
        ipam.nb.ipam.prefixes.filter.return_value = []

        self.assertEqual(ipam._get_prefixes("pubkey", 4), [])
        ipam.nb.ipam.prefixes.filter.assert_called_once_with(
            family=4,
            within="10.0.0.0/8",
            description__ic="pubkey",
        )

        class FakePrefix:
            id = 1
            prefix = "10.0.0.0/24"
            description = '{"pubkey":"pubkey"}'

        allocated = FakePrefix()
        ipam.parent_prefix_v4.available_prefixes.create.return_value = allocated
        with (
            mock.patch.object(
                ipam, "_deduplicate_prefixes", side_effect=[None, allocated]
            ),
            mock.patch(
                "wgkex.broker.ipam_netbox.pynetbox.models.ipam.Prefixes",
                FakePrefix,
            ),
        ):
            prefix, _ = ipam._get_or_allocate_prefix("pubkey", 4, 24, None)

        self.assertIs(prefix, allocated)
        ipam.parent_prefix_v4.available_prefixes.create.assert_called_once()
        ipam.parent_prefix_v6.available_prefixes.create.assert_not_called()

    def test_parent_prefix_rejects_unavailable_or_invalid_family(self):
        ipam = object.__new__(NetboxIPAM)
        ipam.parent_prefix_v4 = None
        ipam.parent_prefix_v6 = mock.MagicMock()

        with self.assertRaisesRegex(ValueError, "No parent IPv4"):
            ipam._parent_prefix(4)
        with self.assertRaisesRegex(ValueError, "Unsupported address family"):
            ipam._parent_prefix(5)


if __name__ == "__main__":
    unittest.main()
