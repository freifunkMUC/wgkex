import ipaddress
import json
import mock
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


if __name__ == "__main__":
    unittest.main()
