import fcntl
import ipaddress
import json
import os
import stat
import tempfile
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
    def __init__(self, file_path: str = FILE_PATH) -> None:
        self.file_path = file_path

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

        storage_dir = os.path.dirname(self.file_path) or "."
        os.makedirs(storage_dir, exist_ok=True)

        with open(f"{self.file_path}.lock", "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                ranges: Dict[str, str] = {}
                parent_prefix = ipaddress.IPv6Network("2001:db8:ed0::/56")
                try:
                    with open(self.file_path, "r", encoding="utf-8") as ranges_file:
                        json_content = json.load(ranges_file)
                        ranges = json_content.get("ranges", {})
                        parent_prefix = ipaddress.IPv6Network(
                            json_content.get("parent_prefix", parent_prefix)
                        )
                except FileNotFoundError:
                    pass
                except json.JSONDecodeError as error:
                    logger.error(
                        "Could not decode JSON IPAM storage %s",
                        self.file_path,
                        exc_info=error,
                    )
                    raise

                range6 = ranges.get(pubkey)
                if range6 is None or not ipaddress.IPv6Network(range6).subnet_of(
                    parent_prefix
                ):
                    parsed_ranges = [
                        ipaddress.IPv6Network(stored_range)
                        for stored_range in ranges.values()
                        if ipaddress.IPv6Network(stored_range).subnet_of(parent_prefix)
                    ]
                    range6 = None

                    prefixes = parent_prefix.subnets(new_prefix=ipv6_prefix_length)
                    next(prefixes)  # Reserve the first prefix for infrastructure.
                    for candidate in prefixes:
                        # Check for overlap instead of equality: stored ranges
                        # may have a different length than the currently
                        # configured one (e.g. after a config change).
                        if not any(
                            candidate.overlaps(stored) for stored in parsed_ranges
                        ):
                            range6 = str(candidate)
                            break
                    if range6 is None:
                        logger.error(
                            "No IPv6 range available for public key %s.", pubkey
                        )
                        return None, None, []

                    logger.info(
                        "No existing IPv6 range found for public key %s, assigning %s",
                        pubkey,
                        range6,
                    )
                    ranges[pubkey] = range6
                    self._atomic_write(parent_prefix, ranges, storage_dir)

                return None, ipaddress.IPv6Network(range6), []
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _atomic_write(
        self,
        parent_prefix: ipaddress.IPv6Network,
        ranges: Dict[str, str],
        storage_dir: str,
    ) -> None:
        existing_mode: Optional[int] = None
        try:
            existing_mode = stat.S_IMODE(os.stat(self.file_path).st_mode)
        except FileNotFoundError:
            pass

        fd, temporary_path = tempfile.mkstemp(
            dir=storage_dir, prefix=".ipv6_ranges.", text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as ranges_file:
                json.dump(
                    {"parent_prefix": str(parent_prefix), "ranges": ranges},
                    ranges_file,
                )
                ranges_file.flush()
                os.fsync(ranges_file.fileno())
            if existing_mode is not None:
                os.chmod(temporary_path, existing_mode)
            os.replace(temporary_path, self.file_path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)

    def release_prefix(self, pubkey: str) -> None:
        raise NotImplementedError

    def update_prefix(
        self, pubkey: str, ipv4: bool, ipv6: bool, selected_concentrators: List[str]
    ) -> None:
        """No-op for JSON file IPAM"""
        pass
