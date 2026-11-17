import ipaddress
import json
import os
import tempfile
import threading
import unittest

from wgkex.broker import ipam_cache
from wgkex.broker.ipam_cache import Entry, IPAMCache


class FakeNetbox:
    """A NetBox that is shared by all brokers in a test."""

    def __init__(self, size=64):
        self.free = [
            str(ipaddress.IPv6Network(f"2001:db8:0:{i * 2:x}::/63"))
            for i in range(size)
        ]
        self.owner: dict[str, dict] = {}  # prefix -> description
        self.next_id = 0
        self.ids: dict[str, int] = {}
        self.up = True
        self.calls: list[str] = []

    def _check(self, name):
        self.calls.append(name)
        if not self.up:
            raise ConnectionError("NetBox is down")

    def _take(self, desc):
        prefix = self.free.pop(0)
        self.owner[prefix] = desc
        self.ids[prefix] = self.next_id
        self.next_id += 1
        return prefix

    def _of(self, pubkey):
        return sorted(
            (p for p, d in self.owner.items() if d.get("pubkey") == pubkey),
            key=self.ids.get,
        )

    @staticmethod
    def _entry(prefix, desc):
        return {
            "prefix": prefix,
            "concentrators": desc.get("selected_concentrators", []),
            "primary": desc.get("primary", False),
            "written": 0.0,
        }

    def lookup(self, pubkey):
        self._check("lookup")
        found = self._of(pubkey)
        return self._entry(found[0], self.owner[found[0]]) if found else None

    def allocate(self, pubkey, prefix_length):
        self._check("allocate")
        found = self._of(pubkey)
        prefix = found[0] if found else self._take({"pubkey": pubkey})
        return self._entry(prefix, self.owner[prefix])

    def store(self, pubkey, prefix, concentrators, primary):
        self._check("store")
        desc = self.owner.get(prefix)
        if desc is None or desc.get("pubkey", pubkey) != pubkey:
            raise ValueError("not this node")
        self.owner[prefix] = {
            "pubkey": pubkey,
            "selected_concentrators": concentrators,
            "primary": primary,
        }
        # Like _deduplicate_prefixes: keep the oldest prefix of the node.
        canonical, *duplicates = self._of(pubkey)
        for duplicate in duplicates:
            del self.owner[duplicate]
        return canonical

    def dump(self, broker_id):
        self._check("dump")
        entries, reserved = {}, []
        for prefix in sorted(self.owner, key=self.ids.get):
            desc = self.owner[prefix]
            if desc.get("reserved_by") == broker_id:
                reserved.append(prefix)
            elif "pubkey" in desc:
                entries.setdefault(desc["pubkey"], self._entry(prefix, desc))
        return entries, reserved

    def reserve(self, broker_id, count, prefix_length):
        self._check("reserve")
        return [self._take({"reserved_by": broker_id}) for _ in range(count)]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _net(prefix):
    return ipaddress.IPv6Network(prefix)


class IPAMCacheTest(unittest.TestCase):
    def setUp(self):
        self.netbox = FakeNetbox()
        self.clock = Clock()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.messages = []

    def cache(self, broker="b1", reserve=4, snapshot=True, notify=None, **kwargs):
        def factory():
            if not self.netbox.up:
                raise ConnectionError("NetBox is down")
            return self.netbox

        return IPAMCache(
            "muc",
            factory,
            broker,
            notify=notify or (lambda *msg: self.messages.append(msg)),
            snapshot_file=(
                os.path.join(self.tmp.name, f"{broker}.json") if snapshot else None
            ),
            reserve=reserve,
            clock=self.clock,
            wall_clock=self.clock,
            **kwargs,
        )

    def get(self, cache, pubkey):
        return cache.get_or_allocate_prefix(pubkey, ipv4=False, ipv6=True)

    def netbox_down(self):
        self.netbox.up = False

    def netbox_up(self):
        self.netbox.up = True
        self.clock.now += ipam_cache._DOWN_TIME

    def test_start_loads_netbox_and_reserves_block(self):
        self.netbox.owner["2001:db8:1::/63"] = {"pubkey": "old", "primary": True}
        self.netbox.ids["2001:db8:1::/63"] = -1
        cache = self.cache()
        self.assertEqual(cache.count_primary(), 1)
        self.assertEqual(len(cache._block), 4)
        self.netbox.calls.clear()
        self.assertEqual(self.get(cache, "old")[1], _net("2001:db8:1::/63"))
        self.assertTrue(cache.is_primary("old"))
        # A known node is answered from memory only.
        self.assertEqual(self.netbox.calls, [])

    def test_miss_reads_netbox_then_allocates_and_notifies(self):
        cache = self.cache()
        self.netbox.allocate("other-broker-node", 63)
        self.netbox.calls.clear()
        self.assertEqual(
            self.get(cache, "other-broker-node")[1],
            _net(self.netbox._of("other-broker-node")[0]),
        )
        self.assertEqual(self.netbox.calls, ["lookup"])
        self.assertEqual(self.messages, [])

        self.netbox.calls.clear()
        _, prefix, _ = self.get(cache, "new")
        self.assertEqual(self.netbox.calls, ["lookup", "allocate"])
        self.assertEqual(self.netbox._of("new"), [str(prefix)])
        self.assertEqual(
            self.messages,
            [("muc", "new", Entry(str(prefix)).to_dict())],
        )

    def test_netbox_down_serves_known_nodes_and_uses_block(self):
        cache = self.cache()
        _, known, _ = self.get(cache, "known")
        block = list(cache._block)
        self.netbox_down()

        self.assertEqual(self.get(cache, "known")[1], known)
        _, prefix, _ = self.get(cache, "new")
        self.assertEqual(str(prefix), block[0])
        self.assertEqual(cache._block, block[1:])
        # The broker does not wait for NetBox again during the down time.
        self.netbox.calls.clear()
        self.get(cache, "new2")
        self.get(cache, "new3")
        self.assertEqual(self.netbox.calls, [])
        cache.update_prefix("known", False, True, ["gw1"], primary=True)
        self.assertEqual(cache._dirty, {"new", "new2", "new3", "known"})
        cache.flush()
        self.assertEqual(len(cache._dirty), 4)

        self.netbox_up()
        cache.sync()
        self.assertEqual(cache._dirty, set())
        self.assertEqual(self.netbox._of("new"), [block[0]])
        self.assertEqual(
            self.netbox.owner[str(known)]["selected_concentrators"], ["gw1"]
        )
        self.assertTrue(self.netbox.owner[str(known)]["primary"])
        # The used prefixes left the block. Less than half was left, so it is full again.
        self.assertEqual(len(cache._block), 4)
        self.assertNotIn(block[0], cache._block)

    def test_netbox_down_and_empty_block_gives_no_prefix(self):
        cache = self.cache(reserve=0)
        self.netbox_down()
        self.assertEqual(self.get(cache, "new"), (None, None, []))

    def test_restart_while_netbox_down_loads_snapshot(self):
        cache = self.cache()
        _, known, _ = self.get(cache, "known")
        cache.update_prefix("known", False, True, ["gw1"], primary=True)
        self.netbox_down()
        _, offline, _ = self.get(cache, "offline")
        cache.save()

        restarted = self.cache()
        self.assertEqual(self.get(restarted, "known"), (None, known, ["gw1"]))
        self.assertEqual(self.get(restarted, "offline")[1], offline)
        self.assertEqual(restarted.count_primary(), 1)
        self.assertEqual(restarted._dirty, {"known", "offline"})
        self.assertEqual(restarted._block, cache._block)
        self.netbox_up()
        restarted.sync()
        self.assertEqual(self.netbox._of("offline"), [str(offline)])

    def test_broken_snapshot_is_ignored(self):
        with open(os.path.join(self.tmp.name, "b1.json"), "w") as file:
            file.write("{")
        self.netbox_down()
        cache = self.cache()
        self.assertEqual(cache._entries, {})

    def test_snapshot_is_written_atomically(self):
        cache = self.cache()
        self.get(cache, "node")
        cache.save()
        with open(cache.snapshot_file) as file:
            data = json.load(file)
        self.assertEqual(set(data["entries"]), {"node"})
        self.assertEqual(data["broker_id"], "b1")
        self.assertFalse(os.path.exists(f"{cache.snapshot_file}.tmp"))

    def test_emqx_down_still_answers(self):
        def fail(*_):
            raise TimeoutError("no PUBACK")

        cache = self.cache(notify=fail)
        self.assertIsNotNone(self.get(cache, "new")[1])

    def test_brokers_share_changes_and_survive_a_broker_down(self):
        b1 = self.cache("b1")
        b2 = self.cache("b2")
        b1.notify = lambda site, key, entry: b2.apply(key, entry)
        b2.notify = lambda site, key, entry: b1.apply(key, entry)

        _, prefix, _ = self.get(b1, "node")
        b1.update_prefix("node", False, True, ["gw1"], primary=True)
        # b2 knows the node without NetBox.
        self.netbox_down()
        self.assertEqual(self.get(b2, "node"), (None, prefix, ["gw1"]))
        self.assertTrue(b2.is_primary("node"))

        # b1 is down. b2 serves the node and writes its changes.
        b2.notify = None
        b2.update_prefix("node", False, True, ["gw2"])
        self.netbox_up()
        b2.sync()
        self.assertEqual(
            self.netbox.owner[str(prefix)]["selected_concentrators"], ["gw2"]
        )

    def test_sync_keeps_recent_changes_that_netbox_does_not_have(self):
        cache = self.cache()
        cache.apply("remote", Entry("2001:db8:ff::/63", ("gw",)).to_dict())
        cache.sync()
        self.assertIn("remote", cache._entries)
        self.clock.now += cache.grace
        cache.sync()
        self.assertNotIn("remote", cache._entries)

    def test_update_prefix_writes_only_changes(self):
        cache = self.cache()
        self.get(cache, "node")
        cache.update_prefix("node", False, True, [])
        self.assertEqual(cache._dirty, set())
        self.assertEqual(self.messages[1:], [])

        cache.update_prefix("node", False, True, ["gw1"], primary=True)
        self.assertEqual(cache._dirty, {"node"})
        self.assertEqual(len(self.messages), 2)
        cache.flush()
        cache.update_prefix("node", False, True, ["gw1"])
        self.assertEqual(cache._dirty, set())

        # Refresh the NetBox time stamp once per day without a notification.
        self.clock.now += ipam_cache._REFRESH_AGE
        cache.update_prefix("node", False, True, ["gw1"])
        self.assertEqual(cache._dirty, {"node"})
        self.assertEqual(len(self.messages), 2)

    def test_concurrent_allocation_converges_to_one_prefix(self):
        b1 = self.cache("b1")
        b2 = self.cache("b2")
        self.netbox_down()
        _, p1, _ = self.get(b1, "node")
        _, p2, _ = self.get(b2, "node")
        self.assertNotEqual(p1, p2)

        self.netbox_up()
        self.messages.clear()
        b1.flush()
        b2.flush()
        self.assertEqual(self.netbox._of("node"), [str(p1)])
        # b2 learns the canonical prefix and tells the other brokers.
        self.assertEqual(self.get(b2, "node")[1], p1)
        self.assertEqual(
            self.messages, [("muc", "node", b2._entries["node"].to_dict())]
        )

    def test_write_for_foreign_prefix_is_dropped(self):
        cache = self.cache()
        cache.apply("node", Entry("2001:db8:ff::/63").to_dict())
        cache._dirty.add("node")
        cache.flush()
        self.assertEqual(cache._dirty, set())

    def test_ipv4_is_not_supported(self):
        cache = self.cache(snapshot=False)
        with self.assertRaises(NotImplementedError):
            cache.get_or_allocate_prefix("node", ipv4=True, ipv6=True)

    def test_run_flushes_and_stops(self):
        cache = self.cache(sync_interval=0)
        stop = threading.Event()
        calls = []
        cache.sync = lambda: (calls.append("sync"), stop.set())
        cache.run(stop, tick=0)
        self.assertEqual(calls, ["sync"])


if __name__ == "__main__":
    unittest.main()
