"""OMNI 后端共享基础设施包。

两个后端（``link`` / ``bus``）都可以 import 本包；本包不得 import 任何一方。
"""

from . import omni_service  # noqa: F401  （对外暴露 common.omni_service）

__all__ = ["omni_service"]
