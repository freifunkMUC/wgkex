import json
from datetime import datetime, timezone
from ipaddress import IPv4Network, IPv6Network
from typing import Any, List, Optional, Tuple

import pynetbox
import pynetbox.core.query
import pynetbox.models.ipam

from wgkex.broker.ipam import ParkerIPAM
from wgkex.common import logger
from wgkex.config import config


class NetboxIPAM(ParkerIPAM):
    nb: pynetbox.api
    xlat: bool
    parent_prefix_v4: Optional[pynetbox.models.ipam.Prefixes]
    parent_prefix_v6: pynetbox.models.ipam.Prefixes

    def __init__(self, api_url, token, xlat: bool = False) -> None:
        self.nb = pynetbox.api(api_url, token=token)
        self.xlat = xlat

        v6_filter: dict[str, Any] = (
            config.get_config().parker.prefixes.ipv6.netbox_filter or {}
        )
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

    def _get_prefix(
        self,
        pubkey: str,
        addr_family: int,
    ) -> Optional[pynetbox.models.ipam.Prefixes]:
        # Look for existing prefixes first
        # https://netboxlabs.com/docs/netbox/reference/filtering/#string-fields
        candidate_prefixes = self.nb.ipam.prefixes.filter(
            family=addr_family,
            within=self.parent_prefix_v6.prefix,
            description__ic=pubkey,  # __ic: Contains (case-insensitive)
        )

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
                return candidate

        return None

    def _get_or_allocate_prefix(
        self,
        pubkey: str,
        addr_family: int,
        prefix_length: int,
        additional_data: Optional[dict[str, Any]],
    ) -> Tuple[Optional[pynetbox.models.ipam.Prefixes], List[str]]:
        prefix: Optional[pynetbox.models.ipam.Prefixes] = None
        selected_concentrators: List[str] = []

        prefix = self._get_prefix(pubkey, addr_family)

        if prefix is not None:
            desc = json.loads(prefix.description)

            selected_concentrators = desc.get("selected_concentrators", None)
            if not isinstance(selected_concentrators, List):
                logger.warning(
                    "Ignoring invalid selected_concentrators for prefix %s pubkey %s: not a list",
                    prefix.prefix,
                    pubkey,
                )
                selected_concentrators = []
            # We don't update "last_allocated_on" here to save a request. Instead require the caller to call update_prefix() later

        # No suitable candidate found, allocate a new prefix
        # TODO support "no-create" mode to only allow known nodes
        if prefix is None:
            # https://demo.netbox.dev/api/schema/swagger-ui/#/ipam/ipam_prefixes_available_prefixes_create
            description = json.dumps(
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
                res = self.parent_prefix_v6.available_prefixes.create(
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
            prefix = res

        return prefix, selected_concentrators

    # TODO consider caching to reduce load on NetBox? Would enough nodes be cached before mass reconnect events happen?
    # Possibly only really affecting notorious unstable nodes that reconnect often
    def get_or_allocate_prefix(
        self,
        pubkey: str,
        ipv4: bool,
        ipv6: bool,
        ipv4_prefix_length: int = 22,
        ipv6_prefix_length: int = 63,
    ) -> Tuple[Optional[IPv4Network], Optional[IPv6Network], List[str]]:
        ipv6_prefix: Optional[pynetbox.models.ipam.Prefixes] = None
        ipv4_prefix: Optional[pynetbox.models.ipam.Prefixes] = None
        ipv6_selected_concentrators: List[str] = []
        ipv4_selected_concentrators: List[str] = []
        selected_concentrators: List[str] = []

        if ipv6:
            ipv6_prefix, ipv6_selected_concentrators = self._get_or_allocate_prefix(
                pubkey,
                addr_family=6,
                prefix_length=ipv6_prefix_length,
                additional_data=config.get_config().parker.prefixes.ipv6.netbox_additional_data,
            )
        if ipv4 and not self.xlat:
            assert (
                self.parent_prefix_v4 is not None
            ), "464XLAT disabled but no parent IPv4 prefix loaded"

            ipv4_prefix, ipv4_selected_concentrators = self._get_or_allocate_prefix(
                pubkey,
                addr_family=4,
                prefix_length=ipv4_prefix_length,
                additional_data=config.get_config().parker.prefixes.ipv4.netbox_additional_data,
            )

        if ipv6_selected_concentrators:
            if (
                ipv4_selected_concentrators
                and ipv4_selected_concentrators != ipv6_selected_concentrators
            ):
                # The selected concentratos stored in the prefixes differ between the IPv6 and IPv4 prefix
                # This is unwanted behaviour, log a warning. We'll prefer the value from the IPv6 prefix
                logger.warning(
                    "selected_concentrators differs between IPv6 and IPv4 prefix for pubkey %s: IPv6: %s, IPv4: %s",
                    pubkey,
                    ipv6_selected_concentrators,
                    ipv4_selected_concentrators,
                )
            selected_concentrators = ipv6_selected_concentrators
        elif ipv4_selected_concentrators:
            selected_concentrators = ipv4_selected_concentrators

        return (
            IPv4Network(ipv4_prefix.prefix) if ipv4_prefix else None,
            IPv6Network(ipv6_prefix.prefix) if ipv6_prefix else None,
            selected_concentrators,
        )

    def release_prefix(self, pubkey: str) -> None:
        raise NotImplementedError

    def _update_prefix(
        self, pubkey: str, addr_family: int, selected_concentrators: List[str]
    ) -> None:
        prefix = self._get_prefix(pubkey, addr_family)

        if prefix is not None:
            desc = json.loads(prefix.description)
            desc["last_allocated_on"] = datetime.now(tz=timezone.utc).isoformat(
                timespec="seconds"
            )
            desc["selected_concentrators"] = selected_concentrators

            try:
                prefix.description = json.dumps(desc)
                prefix.save()
            except Exception as e:
                logger.warning(
                    "Could not update prefix information on %s, pubkey %s",
                    prefix.prefix,
                    pubkey,
                    exc_info=e,
                )

    def update_prefix(
        self, pubkey: str, ipv4: bool, ipv6: bool, selected_concentrators: List[str]
    ) -> None:
        if ipv4:
            self._update_prefix(pubkey, 4, selected_concentrators)
        if ipv6:
            self._update_prefix(pubkey, 6, selected_concentrators)
