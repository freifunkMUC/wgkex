from logging import basicConfig
from logging import DEBUG
from logging import info as info
from logging import warning as warning
from logging import error as error
from logging import critical as critical
from logging import debug as debug

basicConfig(
    format="%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s",
    datefmt="%Y-%m-%d:%H:%M:%S",
    level=DEBUG,
)
