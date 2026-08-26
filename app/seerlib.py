"""PySeer 第三方库的兼容别名 (旧名 `seerlib`).

本模块仅为兼容旧脚本而保留: 全部实现已迁移到 **`PySeer`**, 这里把它们原样转出,
因此 ``from seerlib import Seer`` 仍可用, 等价于 ``from PySeer import Seer``.
新代码请直接 ``import PySeer``; 完整 API 见 docs/PySeer.md.
"""

from PySeer import (  # noqa: F401
    DEFAULT_BASE,
    Battle,
    Packet,
    Seer,
    SeerError,
    discover_backend,
    get_value,
)

__all__ = ["DEFAULT_BASE", "Packet", "SeerError", "Seer", "Battle",
           "discover_backend", "get_value"]
