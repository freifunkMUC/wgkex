import dataclasses
import ipaddress
from typing import Any, Dict, List

from wgkex.common.utils import is_valid_wg_pubkey
from wgkex.config import config


@dataclasses.dataclass
class ParkerQuery:
    """ParkerQuery represents the expected structure for Parker key exchange requests.

    Attributes:
        v6mtu (int): The maximum transmission unit (MTU) size the client can handle.
        pubkey (str): The client's WireGuard public key, base64 encoded.
        nonce (str): A unique nonce value to prevent replay attacks.
    """

    v6mtu: int
    pubkey: str
    nonce: str

    def __init__(self, v6mtu: int, pubkey: str, nonce: str) -> None:
        self.v6mtu = v6mtu
        self.pubkey = is_valid_wg_pubkey(pubkey)
        self.nonce = nonce

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParkerQuery":
        """Creates a new Query object from dict."""
        v6mtu: int = int(data.get("v6mtu", 1280))
        pubkey: str = is_valid_wg_pubkey(data.get("pubkey", ""))
        nonce: str = data.get("nonce", "")
        return cls(v6mtu=v6mtu, pubkey=pubkey, nonce=nonce)


@dataclasses.dataclass
class ParkerResponse:
    """ParkerResponse represents the response structure for Parker key exchange requests.
    See the following Go struct for reference:

    NodeInfo data
    type NodeInfo struct {
        ID                    *uint64            `json:"id,omitempty" etcd:"id"`
        Concentrators         []ConcentratorInfo `json:"concentrators,omitempty" etcd:"-"`
        ConcentratorsJSON     []byte             `json:"-" etcd:"concentrators"`
        MTU                   *uint64            `json:"mtu,omitempty" etcd:"mtu"`
        Retry                 *uint64            `json:"retry,omitempty" etcd:"retry"`
        WGKeepalive           *uint64            `json:"wg_keepalive,omitempty" etcd:"wg_keepalive"`
        Range4                *string            `json:"range4,omitempty" etcd:"range4"`
        Range6                *string            `json:"range6,omitempty" etcd:"range6"`
        Address4              *string            `json:"address4,omitempty" etcd:"address4"`
        Address6              *string            `json:"address6,omitempty" etcd:"address6"`
        SelectedConcentrators *string            `json:"-" etcd:"selected_concentrators"`
    }

    type ConcentratorInfo struct {
        Address4 string `json:"address4"`
        Address6 string `json:"address6"`
        Endpoint string `json:"endpoint"`
        PubKey   string `json:"pubkey"`
        ID       uint32 `json:"id"`
    }

    Attributes:
        nonce (str): A nonce taken over from the request.
        time (int): Current time as a Unix timestamp in seconds.
        id (str): Unique identifier for the node.
        mtu (int): Maximum Transmission Unit size the node should set on the tunnel interface.
        concentrators (List[Dict[str, str | int]]): List of available concentrators with their details.
        selected_concentrators (str): Space-separated list of concentrator IDs for the node to use (in range 1-64).
        range6 (str): The IPv6 prefix assigned to the node in CIDR notation.
        address6 (str): The IPv6 address assigned to the node (should be within range6).
        xlat_range6 (str): The IPv6 prefix for the node to use for 464XLAT as source prefix (FFMUC addition).
        range4 (str): The IPv4 prefix assigned to the node in CIDR notation.
        address4 (str): The IPv4 address assigned to the node (should be within range4). Defaults to the first
            usable address in range4. Might be changed on the node itself if DAD conflict occurs in a mesh.
        wg_keepalive (int): The keepalive interval for the WireGuard tunnel in seconds that the node should set.
        retry (int): The interval in seconds in which the node recontacts the broker to refresh its configuration.
    """

    nonce: str
    time: int
    id: str
    mtu: int
    concentrators: List[Dict[str, str | int]]
    selected_concentrators: str
    range6: str
    address6: str
    xlat_range6: str
    range4: str = config.get_config().parker.prefixes.ipv4.clat_subnet
    address4: str = str(ipaddress.IPv4Network(range4).network_address + 1)
    wg_keepalive: int = config.get_config().parker.wg_keepalive
    retry: int = config.get_config().parker.retry_interval
