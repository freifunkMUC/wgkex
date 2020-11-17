import ipaddress
import re


def mac2eui64(mac, prefix=None):
    """
    Convert a MAC address to a EUI64 identifier
    or, with prefix provided, a full IPv6 address
    """
    # http://tools.ietf.org/html/rfc4291#section-2.5.1
    eui64 = re.sub(r"[.:-]", "", mac).lower()
    eui64 = eui64[0:6] + "fffe" + eui64[6:]
    eui64 = hex(int(eui64[0:2], 16) | 2)[2:].zfill(2) + eui64[2:]

    if prefix is None:
        return ":".join(re.findall(r".{4}", eui64))
    else:
        try:
            net = ipaddress.ip_network(prefix, strict=False)
            euil = int("0x{}".format(eui64), 16)
            return "{}/{}".format(net[euil], net.prefixlen)
        except Exception:  # pylint: disable=broad-except
            return
