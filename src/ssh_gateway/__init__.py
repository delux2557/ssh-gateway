"""SSH Gateway — an HTTP API in front of one SSH device.

    from ssh_gateway import GatewayCenter, Config
"""

from ._version import __version__
from .center import GatewayCenter
from .config import Config, ConfigError
from .server import build_router, make_server

__all__ = ["GatewayCenter", "Config", "ConfigError", "build_router", "make_server",
           "__version__"]
