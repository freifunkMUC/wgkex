from abc import ABC, abstractmethod
from ipaddress import IPv4Network, IPv6Network


class ParkerIPAM(ABC):

    @abstractmethod
    def get_or_allocate_prefix(
        self,
        pubkey: str,
        ipv4: bool,
        ipv6: bool,
        ipv4_prefix_length: int = 22,
        ipv6_prefix_length: int = 63,
    ) -> tuple[IPv4Network | None, IPv6Network | None, list[str]]:
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
        self,
        pubkey: str,
        ipv4: bool,
        ipv6: bool,
        selected_concentrators: list[str],
        primary: bool | None = None,
    ) -> None:
        """Store the selected concentrators of a node.

        Attributes:
            primary: Multi-site only. Whether this site is the primary site of
                the node. None keeps the stored value.
        """
        raise NotImplementedError

    @abstractmethod
    def is_primary(self, pubkey: str) -> bool:
        """Return whether this site is the stored primary site of the node.
        Does not allocate a prefix."""
        raise NotImplementedError

    @abstractmethod
    def count_primary(self) -> int:
        """Return the number of nodes that have this site as primary site."""
        raise NotImplementedError
