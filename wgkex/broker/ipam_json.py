import ipaddress
import json
import os
from typing import Dict, List, Optional, Tuple

from wgkex.broker.ipam import ParkerIPAM
from wgkex.common import logger

# A simple "IPAM" using a local JSON file (located at /var/local/wgkex/broker/ipv6_ranges.json), mostly for development.

# Expects a JSON file in the following format (where "ranges" should be empty at first and will be populated by wgkex as nodes come online):
# {
#   "parent_prefix": "2001:db8:ed0::/56",
#   "ranges": {
#     "<public_key>": "2001:db8:ed0:0::/63",
#     "<public_key>": "2001:db8:ed0:2::/63"
#   }
# }

FILE_PATH = "/var/local/wgkex/broker/ipv6_ranges.json"


class JSONFileIPAM(ParkerIPAM):
    def get_or_allocate_prefix(
        self,
        pubkey: str,
        ipv4: bool,
        ipv6: bool,
        ipv4_prefix_length: int = 22,
        ipv6_prefix_length: int = 63,
    ) -> Tuple[
        Optional[ipaddress.IPv4Network], Optional[ipaddress.IPv6Network], List[str]
    ]:
        """Returns the IPv6 range for a node with a given public key.
        Only supports 464XLAT mode. Does not support storing the selected gateways"""

        if ipv4:
            raise NotImplementedError(
                "Non-464XLAT mode not implemented in JSONFileIPAM"
            )

        if not ipv6:
            return None, None, []

        ranges: Dict[str, str] = {}
        parent_prefix = ipaddress.IPv6Network(
            "2001:db8:ed0::/56"
        )  # Default parent prefix
        try:
            with open(FILE_PATH, "r", encoding="utf-8") as f:
                json_content = json.load(f)
                ranges = json_content.get("ranges", {})
                parent_prefix = ipaddress.IPv6Network(
                    json_content.get("parent_prefix", parent_prefix)
                )
        except FileNotFoundError:
            os.makedirs("/var/local/wgkex/broker", exist_ok=True)
        except json.JSONDecodeError:
            pass

        range6 = ranges.get(pubkey, None)
        if range6 is None or not ipaddress.IPv6Network(range6).subnet_of(parent_prefix):
            parsed_ranges = [
                ipaddress.IPv6Network(rg)
                for rg in ranges.values()
                if ipaddress.IPv6Network(rg).subnet_of(parent_prefix)
            ]  # Filter out any ranges that are not subnets of the parent prefix

            prefixes = parent_prefix.subnets(new_prefix=ipv6_prefix_length)
            next(prefixes)  # skip first
            for candidate in prefixes:
                if candidate not in parsed_ranges:
                    range6 = candidate
                    break
            if range6 is None:
                logger.error(f"No IPv6 range available for public key {pubkey}.")
                return None, None, []
            else:
                logger.info(
                    f"No existing IPv6 range found for public key {pubkey}, assigning {range6}"
                )

            ranges[pubkey] = str(range6)
            with open(FILE_PATH, "w", encoding="utf-8") as f:
                json.dump({"parent_prefix": str(parent_prefix), "ranges": ranges}, f)

        return None, ipaddress.IPv6Network(range6), []

    def release_prefix(self, pubkey: str) -> None:
        raise NotImplementedError

    def update_prefix(
        self, pubkey: str, ipv4: bool, ipv6: bool, selected_concentrators: List[str]
    ) -> None:
        """No-op for JSON file IPAM"""
        pass
