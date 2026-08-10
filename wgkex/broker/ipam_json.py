import contextlib
import fcntl
import ipaddress
import json
import os
import stat
import tempfile
from typing import Dict, Iterator, List, Optional, Tuple

from wgkex.broker.ipam import ParkerIPAM
from wgkex.common import logger

# A simple "IPAM" using a local JSON file (located at /var/local/wgkex/broker/ipv6_ranges.json), mostly for development.

# Expects a JSON file in the following format (where "ranges" should be empty at first and will be populated by wgkex as nodes come online):
# {
#   "parent_prefix": "2001:db8:ed0::/56",
#   "ranges": {
#     "<public_key>": "2001:db8:ed0:0::/63",
#     "<public_key>": "2001:db8:ed0:2::/63"
#   },
#   "concentrators": {
#     "<public_key>": ["gw01.example", "gw03.example"]
#   }
# }
# "concentrators" holds the workers last handed out to a node. It is written by
# update_prefix() and is what makes the broker's sticky worker selection work
# across requests; files without the key are read as "nothing selected yet".

FILE_PATH = "/var/local/wgkex/broker/ipv6_ranges.json"


class JSONFileIPAM(ParkerIPAM):
    def __init__(self, file_path: str = FILE_PATH) -> None:
        self.file_path = file_path

    @contextlib.contextmanager
    def _locked_storage(self) -> Iterator[None]:
        """Serializes access to the storage file."""
        os.makedirs(os.path.dirname(self.file_path) or ".", exist_ok=True)

        with open(f"{self.file_path}.lock", "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _read(
        self,
    ) -> Tuple[ipaddress.IPv6Network, Dict[str, str], Dict[str, List[str]]]:
        """Reads the storage file. Must be called with the storage lock held.

        Returns:
            The parent prefix, the prefixes per public key and the last selected
            concentrators per public key.
        """
        parent_prefix = ipaddress.IPv6Network("2001:db8:ed0::/56")
        ranges: Dict[str, str] = {}
        concentrators: Dict[str, List[str]] = {}

        try:
            with open(self.file_path, "r", encoding="utf-8") as ranges_file:
                json_content = json.load(ranges_file)
        except FileNotFoundError:
            return parent_prefix, ranges, concentrators
        except json.JSONDecodeError as error:
            logger.error(
                "Could not decode JSON IPAM storage %s",
                self.file_path,
                exc_info=error,
            )
            raise

        ranges = json_content.get("ranges", {})
        parent_prefix = ipaddress.IPv6Network(
            json_content.get("parent_prefix", parent_prefix)
        )

        stored_concentrators = json_content.get("concentrators", {})
        if not isinstance(stored_concentrators, dict):
            logger.warning(
                "Ignoring invalid concentrators in JSON IPAM storage %s: not a mapping",
                self.file_path,
            )
            stored_concentrators = {}
        for pubkey, workers in stored_concentrators.items():
            if not isinstance(workers, list) or not all(
                isinstance(worker, str) for worker in workers
            ):
                logger.warning(
                    "Ignoring invalid stored concentrators for public key %s: not a list of names",
                    pubkey,
                )
                continue
            concentrators[pubkey] = workers

        return parent_prefix, ranges, concentrators

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
        """Returns the IPv6 range and the last selected concentrators for a node with a given public key.
        Only supports 464XLAT mode."""

        if ipv4:
            raise NotImplementedError(
                "Non-464XLAT mode not implemented in JSONFileIPAM"
            )

        if not ipv6:
            return None, None, []

        with self._locked_storage():
            parent_prefix, ranges, concentrators = self._read()
            selected_concentrators = concentrators.get(pubkey, [])

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
                    if not any(candidate.overlaps(stored) for stored in parsed_ranges):
                        range6 = str(candidate)
                        break
                if range6 is None:
                    logger.error("No IPv6 range available for public key %s.", pubkey)
                    return None, None, []

                logger.info(
                    "No existing IPv6 range found for public key %s, assigning %s",
                    pubkey,
                    range6,
                )
                ranges[pubkey] = range6
                self._atomic_write(parent_prefix, ranges, concentrators)

            return None, ipaddress.IPv6Network(range6), selected_concentrators

    def _atomic_write(
        self,
        parent_prefix: ipaddress.IPv6Network,
        ranges: Dict[str, str],
        concentrators: Dict[str, List[str]],
    ) -> None:
        existing_mode: Optional[int] = None
        try:
            existing_mode = stat.S_IMODE(os.stat(self.file_path).st_mode)
        except FileNotFoundError:
            pass

        fd, temporary_path = tempfile.mkstemp(
            dir=os.path.dirname(self.file_path) or ".",
            prefix=".ipv6_ranges.",
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as ranges_file:
                json.dump(
                    {
                        "parent_prefix": str(parent_prefix),
                        "ranges": ranges,
                        "concentrators": concentrators,
                    },
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
        """Persists the concentrators handed out to a node, so the next request for
        the same public key can stick to them."""
        if not ipv6:
            return

        try:
            with self._locked_storage():
                parent_prefix, ranges, concentrators = self._read()
                # get_best_workers() iterates a set, so an unchanged selection
                # can arrive in a different order after a broker restart; store
                # it sorted or the comparison below would rewrite the file on
                # every request.
                new_concentrators = sorted(selected_concentrators)

                if concentrators.get(pubkey, []) == new_concentrators:
                    # Nothing changed, don't rewrite the file on every request.
                    return

                if new_concentrators:
                    concentrators[pubkey] = new_concentrators
                else:
                    concentrators.pop(pubkey, None)

                logger.debug(
                    "Persisting selected concentrators %s for public key %s",
                    new_concentrators,
                    pubkey,
                )
                self._atomic_write(parent_prefix, ranges, concentrators)
        except Exception as error:
            # Losing this update only costs the node its sticky concentrators on
            # the next request, so don't fail the response over it. A structurally
            # invalid storage file raises more than OSError/JSONDecodeError.
            logger.error(
                "Could not persist selected concentrators for public key %s in %s",
                pubkey,
                self.file_path,
                exc_info=error,
            )
