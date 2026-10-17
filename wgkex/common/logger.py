import copy
import logging
import os.path
from logging import config, critical, debug, error, info, warning
from typing import Any

__all__ = ["critical", "debug", "error", "info", "warning"]

import yaml

from wgkex.config.config import WG_CONFIG_DEFAULT_LOCATION, WG_CONFIG_OS_ENV

_LOGGING_DEFAULT_CONFIG = {
    "version": 1,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        }
    },
    "formatters": {
        "standard": {
            "format": "%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s"
        },
    },
    "root": {"level": "INFO", "handlers": ["console"]},
}


def fetch_logging_configuration(config_file: str | None = None) -> dict[str, Any]:
    """Fetches logging configuration from disk, if exists.

    If the config file sets 'logging_config', it is used, otherwise the default configuration
    (_LOGGING_DEFAULT_CONFIG). If the config file sets 'log_level', it overrides the level of the root logger.

    Arguments:
        config_file: The configuration file, defaults to $WGKEX_CONFIG_FILE or /etc/wgkex.yaml.

    Returns:
        Logging configuration.
    """
    if config_file is None:
        config_file = os.environ.get(WG_CONFIG_OS_ENV, WG_CONFIG_DEFAULT_LOCATION)
    raw = {}
    if os.path.isfile(config_file):
        with open(config_file) as cfg_file:
            raw = yaml.safe_load(cfg_file)
    if not isinstance(raw, dict):
        raw = {}
    logging_cfg = copy.deepcopy(raw.get("logging_config") or _LOGGING_DEFAULT_CONFIG)
    log_level = raw.get("log_level")
    if log_level:
        level = str(log_level).upper()
        if level in logging.getLevelNamesMapping():
            logging_cfg.setdefault("root", {})["level"] = level
    return logging_cfg


cfg = fetch_logging_configuration()
config.dictConfig(cfg)
info("Initialised logger, using configuration: %s", cfg)  # noqa: LOG015
