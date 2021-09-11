from logging import basicConfig
from logging import DEBUG
from logging import info as info
from logging import warning as warning
from logging import error as error
from logging import critical as critical
from logging import debug as debug
from logging import config
import yaml
import os.path
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
    logging_cfg = dict()
    if os.path.isfile(WG_CONFIG_DEFAULT_LOCATION):
        with open(WG_CONFIG_DEFAULT_LOCATION) as cfg_file:
            logging_cfg = yaml.load(cfg_file, Loader=yaml.FullLoader)
    if logging_cfg.get("logging_config"):
        return logging_cfg.get("logging_config")
    return _LOGGING_DEFAULT_CONFIG


cfg = fetch_logging_configuration()
config.dictConfig(cfg)
info("Initialised logger, using configuration: %s", cfg)
