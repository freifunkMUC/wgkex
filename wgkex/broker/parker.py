import dataclasses
import ipaddress
from typing import Any, Dict, List

from wgkex.common.utils import is_valid_wg_pubkey
from wgkex.config import config


@dataclasses.dataclass
class ParkerQuery:
    v6mtu: int
    pubkey: str  # client WG pubkey, base64 encoded
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
    nonce: str
    time: int  # current time as unix timestamp in seconds datetime.now(tz=timezone.utc).timestamp()
    # NodeInfo data
    # type NodeInfo struct {
    # 	ID                    *uint64            `json:"id,omitempty" etcd:"id"`
    # 	Concentrators         []ConcentratorInfo `json:"concentrators,omitempty" etcd:"-"`
    # 	ConcentratorsJSON     []byte             `json:"-" etcd:"concentrators"`
    # 	MTU                   *uint64            `json:"mtu,omitempty" etcd:"mtu"`
    # 	Retry                 *uint64            `json:"retry,omitempty" etcd:"retry"`
    # 	WGKeepalive           *uint64            `json:"wg_keepalive,omitempty" etcd:"wg_keepalive"`
    # 	Range4                *string            `json:"range4,omitempty" etcd:"range4"`
    # 	Range6                *string            `json:"range6,omitempty" etcd:"range6"`
    # 	Address4              *string            `json:"address4,omitempty" etcd:"address4"`
    # 	Address6              *string            `json:"address6,omitempty" etcd:"address6"`
    # 	SelectedConcentrators *string            `json:"-" etcd:"selected_concentrators"`
    # }

    # type ConcentratorInfo struct {
    # 	Address4 string `json:"address4"`
    # 	Address6 string `json:"address6"`
    # 	Endpoint string `json:"endpoint"`
    # 	PubKey   string `json:"pubkey"`
    # 	ID       uint32 `json:"id"`
    # }

    id: str
    mtu: int
    address6: str
    concentrators: List[Dict[str, str | int]]
    # selected_concentrators: This value contains a space separated list of the concentrator ids to
    # include in the config response. If it is empty or not set, it will
    # default to return all concentrators. Due to the implementation it is
    # currently only possible to use concentrator ids between 1 and 64.
    selected_contentrators: str
    range6: str  # TODO take from IPAM
    xlat_range6: str  # FFMUC addition
    range4: str = (
        config.get_config().parker.prefixes.ipv4.clat_subnet
    )  # type: ignore # always the same with 464XLAT
    address4: str = str(
        ipaddress.IPv4Network(range4).network_address + 1
    )  # default gateway address, but might change after node sees DAD conflict
    wg_keepalive: int = 25
    retry: int = 120
