"""A collection of general utilities."""

import ipaddress
import re

from wgkex.config import config

WG_PUBKEY_PATTERN = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$")


def mac2eui64(mac: str, prefix=None) -> str:
    """Converts a MAC address to an EUI64 identifier.

    If prefix is supplied, further convert the EUI64 address to an IPv6 address.
    eg:
        c4:91:0c:b2:c5:a0 -> c691:0cff:feb2:c5a0
        c4:91:0c:b2:c5:a0, FE80::/10 -> fe80::c691:cff:feb2:c5a0/10

    Arguments:
        mac: The mac address to convert.
        prefix: Prefix to use to create IPv6 address.

    Raises:
        ValueError: If mac or prefix is not correct format.

    Returns:
        An EUI64 address, or IPv6 Prefix.
    """
    if mac.count(":") != 5:
        raise ValueError(
            f"{mac} does not appear to be a correctly formatted mac address"
        )
    # http://tools.ietf.org/html/rfc4291#section-2.5.1
    eui64 = re.sub(r"[.:-]", "", mac).lower()
    eui64 = eui64[0:6] + "fffe" + eui64[6:]
    eui64 = hex(int(eui64[0:2], 16) | 2)[2:].zfill(2) + eui64[2:]

    if not prefix:
        return ":".join(re.findall(r".{4}", eui64))
    net = ipaddress.ip_network(prefix, strict=False)
    euil = int(f"0x{eui64:16}", 16)
    return f"{net[euil]}/{net.prefixlen}"


def is_valid_domain(domain: str) -> bool:
    """Verifies if the domain is configured.

    Arguments:
        domain: The domain to verify.

    Returns:
        True if the domain is valid, False otherwise.
    """
    if domain not in config.get_config().domains:
        return False
    for prefix in config.get_config().domain_prefixes:
        if domain.startswith(prefix):
            return True
    return False


def is_valid_wg_pubkey(pubkey: str) -> str:
    """Verifies if key is a valid WireGuard public key or not.

    Arguments:
        pubkey: The key to verify.

    Raises:
        ValueError: If the Wireguard Key is invalid.

    Returns:
        The public key.
    """
    # TODO(ruairi): Refactor to return bool.
    if WG_PUBKEY_PATTERN.match(pubkey) is None:
        raise ValueError(f"Not a valid Wireguard public key: {pubkey}.")
    return pubkey
