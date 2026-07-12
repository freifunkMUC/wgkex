import ipaddress
import json
import mock
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from wgkex.broker.ipam_netbox import NetboxIPAM
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
    if args[0] == "POST":
        if args[1].endswith("/api/ipam/prefixes/"):
            return MockResponse.with_data(
                status_code=201,
                json_data={
                    "id": 2,
                    "prefix": "2001:db8:999::/63",
                    "description": '{"pubkey": "pubkey"}',
                },
            )
    if args[0] == "PATCH":
        if args[1].endswith("/api/ipam/prefixes/2/"):
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
    def test_get_or_allocate_prefix(self, _):
        # Test allocating new prefix for previously unknown pubkey
        ipv4_network, ipv6_network, selected_concentrators = (
            self.ipam.get_or_allocate_prefix(
                "pubkey", ipv4=False, ipv6=True, ipv6_prefix_length=63
            )
        )
        self.assertIsNone(ipv4_network)
        self.assertEqual(ipv6_network, ipaddress.IPv6Network("2001:db8:99::/63"))
        self.assertEqual(selected_concentrators, [])

    @mock.patch(
        "requests.sessions.Session.request", side_effect=_mocked_netbox_response
    )
    def test_update_prefix(self, mock_response):
        self.ipam.update_prefix(
            "pubkey", False, True, ["concentrator1", "concentrator2"]
        )

        # mock_response.assert_called_once()
        mock_response.assert_any_call(
            "PATCH",
            "http://localhost:54321/api/ipam/prefixes/2/",
            data=mock.ANY,
            headers=mock.ANY,
            params=mock.ANY,
            json=mock.ANY,
        )

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

        with mock.patch.object(
            self.ipam,
            "_get_prefixes",
            return_value=[canonical, duplicate],
        ):
            with self.assertRaises(RuntimeError):
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

    def test_dual_stack_prefers_ipv6_concentrators(self):
        ipam = object.__new__(NetboxIPAM)
        ipam.xlat = False
        ipam.parent_prefix_v4 = mock.MagicMock()
        prefix6 = mock.MagicMock(prefix="2001:db8::/63")
        prefix4 = mock.MagicMock(prefix="10.0.0.0/22")
        ipam._get_or_allocate_prefix = mock.MagicMock(
            side_effect=[
                (prefix6, ["worker-v6"]),
                (prefix4, ["worker-v4"]),
            ]
        )

        prefix4_result, prefix6_result, workers = ipam.get_or_allocate_prefix(
            "pubkey", ipv4=True, ipv6=True
        )
        self.assertEqual(prefix4_result, ipaddress.IPv4Network("10.0.0.0/22"))
        self.assertEqual(prefix6_result, ipaddress.IPv6Network("2001:db8::/63"))
        self.assertEqual(workers, ["worker-v6"])

        ipam._get_or_allocate_prefix.side_effect = [
            (None, []),
            (prefix4, ["worker-v4"]),
        ]
        _, _, workers = ipam.get_or_allocate_prefix("pubkey", ipv4=True, ipv6=True)
        self.assertEqual(workers, ["worker-v4"])

    def test_update_prefix_handles_save_failure_and_both_families(self):
        prefix = mock.MagicMock(
            prefix="2001:db8::/63",
            description='{"pubkey":"pubkey"}',
        )
        prefix.save.side_effect = RuntimeError("save failed")
        with mock.patch.object(self.ipam, "_deduplicate_prefixes", return_value=prefix):
            self.ipam._update_prefix("pubkey", 6, ["worker"])
        self.assertEqual(
            json.loads(prefix.description)["selected_concentrators"], ["worker"]
        )

        with mock.patch.object(self.ipam, "_update_prefix") as update:
            self.ipam.update_prefix("pubkey", True, True, ["worker"])
        update.assert_has_calls(
            [
                mock.call("pubkey", 4, ["worker"]),
                mock.call("pubkey", 6, ["worker"]),
            ]
        )

        with self.assertRaises(NotImplementedError):
            self.ipam.release_prefix("pubkey")


if __name__ == "__main__":
    unittest.main()
