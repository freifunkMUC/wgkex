from abc import ABC, abstractmethod
from ipaddress import IPv4Network, IPv6Network
from typing import Optional, Tuple


class ParkerIPAM(ABC):

    @abstractmethod
    def get_or_allocate_prefix(
        self,
        pubkey: str,
        ipv4: bool,
        ipv6: bool,
        ipv4_prefix_length: int = 22,
        ipv6_prefix_length: int = 63,
    ) -> Tuple[Optional[IPv4Network], Optional[IPv6Network]]:
        """Fetch existing or allocate new IPv4 & IPv6 prefixes for a client identified.
        Does not ensure the prefix lengths of existing prefixes match the requested lengths.
        """
        raise NotImplementedError

    @abstractmethod
    def release_prefix(self, pubkey: str) -> None:
        """Release any previously allocated IP prefix."""
        raise NotImplementedError
