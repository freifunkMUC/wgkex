from abc import ABC, abstractmethod
from ipaddress import IPv4Network, IPv6Network
from typing import List, Optional, Tuple


class ParkerIPAM(ABC):

    @abstractmethod
    def get_or_allocate_prefix(
        self,
        pubkey: str,
        ipv4: bool,
        ipv6: bool,
        ipv4_prefix_length: int = 22,
        ipv6_prefix_length: int = 63,
    ) -> Tuple[Optional[IPv4Network], Optional[IPv6Network], List[str]]:
        """
        Fetch existing prefixes of allocate new ones for this pubkey.
        Does not ensure the prefix lengths of existing prefixes match the requested lengths.
        You must call ParkerIPAM.update_prefix() afterwards as well.

        Attributes:
            pubkey: the node pubkey to fetch the prefixes for
            ipv4: whether to allocate an IPv4 prefix (subnet)
            ipv6: whether to allocate an IPv6 prefix
            ipv4_prefix_length: the prefix length in bits (as in CIDR notation) to allocate
            ipv6_prefix_length: see ipv4_prefix_length

        Returns:
            An IPv4 prefix or None, an IPv6 prefix or None, and a list of previously selected gateways (can be empty)
        """
        raise NotImplementedError

    @abstractmethod
    def release_prefix(self, pubkey: str) -> None:
        """Release any previously allocated IP prefix."""
        raise NotImplementedError

    @abstractmethod
    def update_prefix(
        self, pubkey: str, ipv4: bool, ipv6: bool, selected_concentrators: List[str]
    ) -> None:
        """Release any previously allocated IP prefix."""
        raise NotImplementedError
