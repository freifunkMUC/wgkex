import json
import sys
from datetime import datetime, timezone
from typing import Any

import pynetbox
import pynetbox.core.query
import pynetbox.models.ipam
import requests

from wgkex.common import logger
from wgkex.config import config

# NetBox limits the description of a prefix to 200 characters.
_MAX_DESCRIPTION = 200


def _describe(data: dict[str, Any]) -> str:
    """Return the compact JSON description. Raise ValueError if NetBox would refuse it."""
    description = json.dumps(data, separators=(",", ":"))
    if len(description) > _MAX_DESCRIPTION:
        raise ValueError(
            f"The description has {len(description)} characters, "
            f"NetBox accepts {_MAX_DESCRIPTION}"
        )
    return description


class _TimeoutSession(requests.Session):
    """A session with a default timeout, so that a dead NetBox does not block a request."""

    timeout: float = 5

    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        return super().request(*args, **kwargs)


class NetboxIPAM:
    """The NetBox store of the node prefixes. The broker reads it through IPAMCache."""

    nb: pynetbox.api
    xlat: bool
    parent_prefix_v4: pynetbox.models.ipam.Prefixes | None
    parent_prefix_v6: pynetbox.models.ipam.Prefixes

    def __init__(
        self,
        api_url,
        token,
        xlat: bool = False,
        v6_filter: dict[str, Any] | None = None,
    ) -> None:
        self.nb = pynetbox.api(api_url, token=token)
        self.nb.http_session = _TimeoutSession()
        self.xlat = xlat
        self.parent_prefix_v4 = None

        if v6_filter is None:
            v6_filter = config.get_config().parker.prefixes.ipv6.netbox_filter or {}
        # https://demo.netbox.dev/api/schema/swagger-ui/#/ipam/ipam_prefixes_list
        prefixes_v6 = self.nb.ipam.prefixes.filter(family=6, **v6_filter)
        if len(prefixes_v6) != 1:
            raise ValueError(
                "Could not uniquely identify parent IPv6 prefix for wgkex in NetBox."
            )

        self.parent_prefix_v6 = next(prefixes_v6)

        if not self.xlat:
            if config.get_config().parker.prefixes.ipv4.netbox_filter is None:
                raise ValueError(
                    "464XLAT is disabled but no IPv4 NetBox filter configured."
                )

            v4_filter: dict[str, str] = (
                config.get_config().parker.prefixes.ipv4.netbox_filter
            )  # type: ignore
            prefixes_v4 = self.nb.ipam.prefixes.filter(family=4, **v4_filter)
            if len(prefixes_v4) != 1:
                raise ValueError(
                    "Could not uniquely identify parent IPv4 prefix for wgkex in NetBox."
                )

            self.parent_prefix_v4 = next(prefixes_v4)

    def _parent_prefix(self, addr_family: int) -> pynetbox.models.ipam.Prefixes:
        """Returns the parent prefix for the given address family."""
        if addr_family == 4:
            if self.parent_prefix_v4 is None:
                raise ValueError("No parent IPv4 prefix loaded (464XLAT mode enabled?)")
            return self.parent_prefix_v4
        if addr_family == 6:
            return self.parent_prefix_v6
        raise ValueError(f"Unsupported address family: {addr_family}")

    def _get_prefixes(
        self,
        pubkey: str,
        addr_family: int,
    ) -> list[pynetbox.models.ipam.Prefixes]:
        # https://netboxlabs.com/docs/netbox/reference/filtering/#string-fields
        candidate_prefixes = self.nb.ipam.prefixes.filter(
            family=addr_family,
            within=self._parent_prefix(addr_family).prefix,
            description__ic=pubkey,  # __ic: Contains (case-insensitive)
        )

        matching_prefixes = []
        for candidate in candidate_prefixes:
            try:
                desc = json.loads(candidate.description)
                if not isinstance(desc, dict):
                    raise TypeError
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Could not decode JSON description for prefix %s, pubkey %s. Consider deleting it so it can be reused.",
                    candidate.prefix,
                    pubkey,
                )
                continue
            if desc.get("pubkey") == pubkey:
                matching_prefixes.append(candidate)

        return matching_prefixes

    @staticmethod
    def _prefix_sort_key(prefix: pynetbox.models.ipam.Prefixes) -> tuple[int, str]:
        prefix_id = getattr(prefix, "id", None)
        return (
            int(prefix_id) if prefix_id is not None else sys.maxsize,
            str(prefix.prefix),
        )

    def _deduplicate_prefixes(
        self,
        pubkey: str,
        addr_family: int,
        allocated_prefix: pynetbox.models.ipam.Prefixes | None = None,
    ) -> pynetbox.models.ipam.Prefixes | None:
        candidates = self._get_prefixes(pubkey, addr_family)
        if allocated_prefix is not None and not any(
            getattr(candidate, "id", None) == getattr(allocated_prefix, "id", None)
            for candidate in candidates
        ):
            candidates.append(allocated_prefix)
        if not candidates:
            return None

        canonical = min(candidates, key=self._prefix_sort_key)
        for duplicate in candidates:
            if duplicate is canonical:
                continue
            try:
                if not duplicate.delete():
                    raise RuntimeError(
                        f"NetBox refused to release duplicate prefix "
                        f"{duplicate.prefix} for pubkey {pubkey}"
                    )
            except pynetbox.core.query.RequestError as error:
                if getattr(getattr(error, "req", None), "status_code", None) == 404:
                    continue
                raise RuntimeError(
                    f"Failed to release duplicate prefix {duplicate.prefix} "
                    f"for pubkey {pubkey}"
                ) from error

        return canonical

    def _get_or_allocate_prefix(
        self,
        pubkey: str,
        addr_family: int,
        prefix_length: int,
        additional_data: dict[str, Any] | None,
    ) -> tuple[pynetbox.models.ipam.Prefixes | None, list[str]]:
        prefix: pynetbox.models.ipam.Prefixes | None = None
        selected_concentrators: list[str] = []

        prefix = self._deduplicate_prefixes(pubkey, addr_family)

        if prefix is not None:
            desc = json.loads(prefix.description)

            selected_concentrators = desc.get("selected_concentrators", None)
            if not isinstance(selected_concentrators, list):
                logger.warning(
                    "Ignoring invalid selected_concentrators for prefix %s pubkey %s: not a list",
                    prefix.prefix,
                    pubkey,
                )
                selected_concentrators = []

        # No suitable candidate found, allocate a new prefix
        # TODO support "no-create" mode to only allow known nodes
        if prefix is None:
            # https://demo.netbox.dev/api/schema/swagger-ui/#/ipam/ipam_prefixes_available_prefixes_create
            description = _describe(
                {
                    "pubkey": pubkey,
                    "last_allocated_on": datetime.now(tz=timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "created_by": "wgkex",
                }
            )
            # Allocates a new free prefix in a concurrency-safe manner (handled by NetBox)
            # TODO: allow specifying a custom field to write this data into instead
            try:
                res = self._parent_prefix(addr_family).available_prefixes.create(
                    {
                        "prefix_length": prefix_length,
                        "description": description,
                        "mark_utilized": True,
                        **(additional_data or {}),
                    }
                )
            except pynetbox.core.query.RequestError as e:
                logger.error(
                    "Failed to allocate new prefix for pubkey %s",
                    pubkey,
                    exc_info=e,
                )
                return None, []

            if not isinstance(res, pynetbox.models.ipam.Prefixes):
                logger.error(
                    "Failed to allocate new prefix for pubkey %s, response %s",
                    pubkey,
                    res,
                )
                return None, []
            prefix = self._deduplicate_prefixes(
                pubkey, addr_family, allocated_prefix=res
            )

            desc = json.loads(prefix.description)
            stored_concentrators = desc.get("selected_concentrators", [])
            if isinstance(stored_concentrators, list):
                selected_concentrators = stored_concentrators

        return prefix, selected_concentrators

    def _entry(self, prefix: pynetbox.models.ipam.Prefixes) -> dict[str, Any]:
        """Return the cache entry of a node prefix."""
        desc = json.loads(prefix.description)
        concentrators = desc.get("selected_concentrators", [])
        try:
            written = datetime.fromisoformat(
                desc.get("last_seen") or desc["last_allocated_on"]
            ).timestamp()
        except (KeyError, TypeError, ValueError):
            written = 0.0
        return {
            "prefix": str(prefix.prefix),
            "concentrators": concentrators if isinstance(concentrators, list) else [],
            "primary": desc.get("primary") is True,
            "written": written,
        }

    def lookup(self, pubkey: str) -> dict[str, Any] | None:
        """Return the stored IPv6 entry of a node, or None. Does not allocate."""
        prefix = self._deduplicate_prefixes(pubkey, 6)
        return None if prefix is None else self._entry(prefix)

    def allocate(self, pubkey: str, prefix_length: int) -> dict[str, Any] | None:
        """Return the stored IPv6 entry of a node, or allocate a new prefix."""
        prefix, _ = self._get_or_allocate_prefix(
            pubkey,
            6,
            prefix_length,
            config.get_config().parker.prefixes.ipv6.netbox_additional_data,
        )
        return None if prefix is None else self._entry(prefix)

    def store(
        self, pubkey: str, prefix: str, concentrators: list[str], primary: bool
    ) -> str:
        """Write the entry of a node to its prefix. The prefix can be a reserved one.

        Returns:
            The prefix of the node after the removal of duplicates.

        Raises:
            ValueError: The prefix does not exist or belongs to another node.
        """
        found = list(
            self.nb.ipam.prefixes.filter(
                family=6, within=self.parent_prefix_v6.prefix, prefix=prefix
            )
        )
        if len(found) != 1:
            raise ValueError(f"Prefix {prefix} of {pubkey} is not in NetBox")
        record = found[0]
        try:
            desc = json.loads(record.description or "{}")
        except json.JSONDecodeError:
            desc = {}
        if desc.get("pubkey", pubkey) != pubkey:
            raise ValueError(f"Prefix {prefix} belongs to another node, not {pubkey}")
        now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        # last_seen is also the time of this write; one timestamp keeps the description short.
        record.description = _describe(
            {
                "pubkey": pubkey,
                "last_seen": now,
                "created_by": "wgkex",
                "selected_concentrators": concentrators,
                "primary": primary,
            }
        )
        record.save()
        canonical = self._deduplicate_prefixes(pubkey, 6, allocated_prefix=record)
        return str(canonical.prefix if canonical is not None else prefix)

    def dump(self, broker_id: str) -> tuple[dict[str, dict[str, Any]], list[str]]:
        """Read all node prefixes in the parent prefix.

        Returns:
            Public key -> entry, and the free prefixes that this broker reserved.
        """
        found: dict[str, list[pynetbox.models.ipam.Prefixes]] = {}
        reserved: list[str] = []
        for prefix in self.nb.ipam.prefixes.filter(
            family=6, within=self.parent_prefix_v6.prefix
        ):
            try:
                desc = json.loads(prefix.description or "")
            except json.JSONDecodeError:
                continue
            if not isinstance(desc, dict):
                continue
            if desc.get("reserved_by") == broker_id:
                reserved.append(str(prefix.prefix))
            elif isinstance(desc.get("pubkey"), str):
                found.setdefault(desc["pubkey"], []).append(prefix)
        entries = {}
        for pubkey, prefixes in found.items():
            # Two brokers allocated for the same new node at the same time.
            canonical = (
                self._deduplicate_prefixes(pubkey, 6)
                if len(prefixes) > 1
                else prefixes[0]
            )
            if canonical is not None:
                entries[pubkey] = self._entry(canonical)
        return entries, reserved

    def reserve(self, broker_id: str, count: int, prefix_length: int) -> list[str]:
        """Reserve free prefixes for this broker. It uses them when NetBox is down."""
        description = _describe({"reserved_by": broker_id, "created_by": "wgkex"})
        data = {
            "prefix_length": prefix_length,
            "description": description,
            "mark_utilized": True,
            **(config.get_config().parker.prefixes.ipv6.netbox_additional_data or {}),
        }
        created = self.parent_prefix_v6.available_prefixes.create([data] * count)
        if not isinstance(created, list):
            created = [created]
        return [
            str(item["prefix"] if isinstance(item, dict) else item.prefix)
            for item in created
        ]

    def _node_prefixes(self) -> list[tuple[str, dict[str, Any], Any]]:
        """Return (public key, description, record) of all node prefixes of wgkex."""
        nodes = []
        for prefix in self.nb.ipam.prefixes.filter(
            family=6, within=self.parent_prefix_v6.prefix
        ):
            try:
                desc = json.loads(prefix.description or "")
            except json.JSONDecodeError:
                continue
            if (
                isinstance(desc, dict)
                and desc.get("created_by") == "wgkex"
                and isinstance(desc.get("pubkey"), str)
            ):
                nodes.append((desc["pubkey"], desc, prefix))
        return nodes

    def seen(self) -> dict[str, float | None]:
        """Return public key -> last_seen (UNIX seconds) of all node prefixes of wgkex.

        None: the prefix has no last_seen yet.
        """
        values: dict[str, list[float | None]] = {}
        for pubkey, desc, _ in self._node_prefixes():
            try:
                seen = datetime.fromisoformat(desc["last_seen"]).timestamp()
            except (KeyError, TypeError, ValueError):
                seen = None
            values.setdefault(pubkey, []).append(seen)
        return {
            pubkey: None if None in seen else max(seen)  # type: ignore[type-var]
            for pubkey, seen in values.items()
        }

    def mark_seen(self, pubkeys: set[str]) -> int:
        """Set last_seen to now on the node prefixes of these keys that have no last_seen."""
        now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        count = 0
        for pubkey, desc, record in self._node_prefixes():
            if pubkey in pubkeys and "last_seen" not in desc:
                record.description = _describe({**desc, "last_seen": now})
                record.save()
                count += 1
        return count

    def release(self, pubkey: str) -> list[str]:
        """Delete the node prefixes of a node. Only prefixes that wgkex created."""
        released = []
        for prefix in self._get_prefixes(pubkey, 6):
            if json.loads(prefix.description).get("created_by") != "wgkex":
                continue
            prefix.delete()
            released.append(str(prefix.prefix))
        return released
