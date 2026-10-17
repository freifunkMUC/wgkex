"""Allowlisted keys, which are never removed by the stale peer cleanup or rebalancing/draining."""

import threading

from wgkex.common.keylist import KeyList
from wgkex.config import config

_lock = threading.Lock()
_allowlist: KeyList | None = None


def get_allowlist() -> KeyList:
    """Returns the allowlist from the configuration, created on first use."""
    global _allowlist
    with _lock:
        if _allowlist is None:
            cfg = config.get_config().key_lists
            if isinstance(cfg, config.KeyLists):
                _allowlist = KeyList(
                    "allowlist",
                    keys=cfg.allowlist,
                    file=cfg.allowlist_file,
                    check_interval=cfg.reload_interval,
                )
            else:
                _allowlist = KeyList("allowlist")
        return _allowlist


def is_allowlisted(public_key: str) -> bool:
    return public_key in get_allowlist()


def reset() -> None:
    """Forgets the loaded allowlist, for tests."""
    global _allowlist
    with _lock:
        _allowlist = None
