# Import _speed first: it installs uvloop as the global asyncio policy on
# POSIX before any other module gets a chance to grab the default loop.
from . import _speed  # noqa: F401
from .errors import TrunksError
from .sdk import Trunks
from .trunk import Trunk
from .version import __version__

__all__ = ["Trunk", "Trunks", "TrunksError", "__version__"]
