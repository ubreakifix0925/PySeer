# -*- coding: utf-8 -*-
"""精灵头像解析入口 (手动/命令行用).

依赖 UnityPy, 但无需手动安装: 运行时会自动 pip 安装 UnityPy 到项目 vendor/ 目录.
用法: python3 tools/extract_pet_heads.py [cache_dir] [out_dir]
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)
if _PROJ not in sys.path:
    sys.path.insert(0, _PROJ)

from assets_updater import _ensure_unitypy, extract_pet_avatars, log  # noqa: E402


def main():
    cache_dir = sys.argv[1] if len(sys.argv) > 1 else str(
        os.path.join(_PROJ, "cache", "pet_head"))
    out_dir = sys.argv[2] if len(sys.argv) > 2 else str(
        os.path.join(_PROJ, "refs", "head"))
    try:
        _ensure_unitypy()
        n = extract_pet_avatars(cache_dir, out_dir)
        return 0 if n > 0 else 3
    except Exception as e:
        log(f"解析失败: {e}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
