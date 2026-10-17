import os.path
from logging import config, critical, debug, error, info, warning

__all__ = ["critical", "debug", "error", "info", "warning"]

import yaml

from wgkex.config.config import WG_CONFIG_DEFAULT_LOCATION

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
    "root": {"level": "DEBUG", "handlers": ["console"]},
}


def fetch_logging_configuration():
    """Fetches logging configuration from disk, if exists.

    If the config exists, then we check to see if the key 'logging_config' is set. If it is, we return this configuration.
    Otherwise, we return the default configuration (_LOGGING_DEFAULT_CONFIG).

    Returns:
        Logging configuration.
    """
    logging_cfg = {}
    if os.path.isfile(WG_CONFIG_DEFAULT_LOCATION):
        with open(WG_CONFIG_DEFAULT_LOCATION) as cfg_file:
            logging_cfg = yaml.load(cfg_file, Loader=yaml.FullLoader)
    if logging_cfg.get("logging_config"):
        return logging_cfg.get("logging_config")
    return _LOGGING_DEFAULT_CONFIG


cfg = fetch_logging_configuration()
config.dictConfig(cfg)
info("Initialised logger, using configuration: %s", cfg)  # noqa: LOG015
