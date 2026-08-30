# -*- coding: utf-8 -*-
"""精灵头像自动更新器: 自动检查/下载/用 UnityPy 解析, 全程纯 Python.

启动时调用 :func:`ensure_pet_avatars`:
  1. 读远端 YooAsset 版本号 (PackageManifest_<pkg>.version)
  2. 与本地记录 (data/head/.avatar_state.json) 比对; 版本一致且头像目录非空 -> 跳过
  3. 否则: 自动安装 UnityPy(如缺失) -> 下载 pet_head_*.bundle 到缓存
           -> 用 UnityPy 解析出 <物种id>.png 写入 data/head/ -> 记录版本

- 依赖自动安装: 优先用已存在的 pip; 没有则引导安装 pip(get-pip.py), 再把 UnityPy
  装到项目内 vendor/ 目录并加入 sys.path, 不污染全局 site-packages.
- 下载用 stdlib urllib; 清单解析用 stdlib struct.
"""
import hashlib
import json
import os
import re
import shlex
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ITEM_NAME_CJK = re.compile(r"[\u4e00-\u9fff]")

# ---- 可配置 ---- (可用环境变量覆盖)
PKG = os.environ.get("SEER_AVATAR_PKG", "DefaultPackage")
REMOTE_BASE = os.environ.get(
    "SEER_AVATAR_BASE",
    "https://newseer.61.com/Assets/StandaloneWindows64/DefaultPackage/",
)
BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))   # 源码目录 app/
_PROJ = BASE_DIR.parent                                        # 项目根目录
_DATA_DIR = _PROJ / "data"                                     # 运行时资源目录
HEAD_DIR = Path(os.environ.get("SEER_AVATAR_OUT", _DATA_DIR / "head"))
CACHE_DIR = Path(os.environ.get("SEER_AVATAR_CACHE", _PROJ / "cache" / "pet_head"))
VENDOR_DIR = Path(os.environ.get("SEER_AVATAR_VENDOR", _PROJ / "vendor"))
STATE_FILE = HEAD_DIR / ".avatar_state.json"
BUNDLE_GLOB = "_pet_head_"  # 匹配"精灵头像" bundle 名

# ---- petbook (精灵图鉴名字) 相关 ----
PETBOOK_FILE = Path(os.environ.get("SEER_PETBOOK_OUT", _DATA_DIR / "petbook.json"))
PETBOOK_STATE = Path(os.environ.get("SEER_PETBOOK_STATE", _DATA_DIR / ".petbook_state.json"))

# 全物种名兜底表 (monster_names.json):
# petbook.bytes 是"图鉴"表, 只覆盖 id <= 5000 的 4901 个物种; 而 monsters.bytes 里
# 还有大量 id > 5000 的物种(如 5357/15003...), 它们**有属性但没名字**, 界面只能显示 (id=xxxx)。
# monsters.bytes 每条记录都带 def_name, 用它可以把这些名字补齐 —— 与图鉴名并存,
# 由 webui 按优先级合并(图鉴名 > 本表)。
MONSTER_NAMES_FILE = Path(os.environ.get(
    "SEER_MONSTERNAMES_OUT", _DATA_DIR / "monster_names.json"))
MONSTER_NAMES_STATE = Path(os.environ.get(
    "SEER_MONSTERNAMES_STATE", _DATA_DIR / ".monster_names_state.json"))
CONFIG_PKG = os.environ.get("SEER_PETBOOK_PKG", "ConfigPackage")
CONFIG_BASE = os.environ.get(
    "SEER_PETBOOK_BASE",
    "https://newseer.61.com/Assets/StandaloneWindows64/ConfigPackage/",
)
CONFIG_CACHE = Path(os.environ.get("SEER_PETBOOK_CACHE", _PROJ / "cache" / "petbook"))
PETBOOK_ASSET = "assets/game/configs/bytes/petbook.bytes"  # 图鉴二进制表(含 id->名字)
MONSTERS_ASSET = "assets/game/configs/bytes/monsters.bytes"  # 精灵二进制表(含 id,real_id,type,技能等)
SKILLTYPES_ASSET = "assets/game/configs/bytes/skilltypes.bytes"  # 精灵属性类型表(id->中文名, 含双属性/新属性)
MOVES_ASSET = "assets/game/configs/bytes/moves.bytes"  # 技能基础表(id->名/pp/属性/威力/命中/暴击/必中/先制/效果)
SKILL_EFFECT_ASSET = "assets/game/configs/bytes/skill_effect.bytes"  # 技能效果表(id->描述模板/参数个数)
SKILLS_FILE = Path(os.environ.get(
    "SEER_SKILLS_OUT", _DATA_DIR / "skills.json"))
SKILLS_STATE = Path(os.environ.get(
    "SEER_SKILLS_STATE", _DATA_DIR / ".skills_state.json"))
EFFECT_ICON_ASSET = "assets/game/configs/bytes/effecticon.bytes"  # 魂印/效果图标表(id->描述/pet_id关联/效果)
EFFECT_TAG_ASSET = "assets/game/configs/bytes/effectag.bytes"  # 魂印效果标签表(id->标签名)
SOULMARKS_FILE = Path(os.environ.get(
    "SEER_SOULMARKS_OUT", _DATA_DIR / "soulmarks.json"))
SOULMARKS_STATE = Path(os.environ.get(
    "SEER_SOULMARKS_STATE", _DATA_DIR / ".soulmarks_state.json"))
EFFECT_ICON_DIR = Path(os.environ.get(
    "SEER_EFFECTICON_DIR", _DATA_DIR / "effecticon"))  # 魂印/效果图标输出目录
EFFECT_ICON_GLOB = "_effecticon_"  # 匹配 DefaultPackage 里"效果图标" bundle
PET_ATTR_FILE = Path(os.environ.get("SEER_PETATTR_OUT", _DATA_DIR / "pet_attr.json"))
PET_ATTR_STATE = Path(os.environ.get(
    "SEER_PETATTR_STATE", _DATA_DIR / ".pet_attr_state.json"))
MONSTERS_JSON = Path(os.environ.get(
    "SEER_MONSTERS_JSON", _PROJ / "refs" / "monsters.json"))

# ---- 物品名 (itemsoptimizecatitems{N}.bytes) 相关 ----
# 物品定义分布在 ConfigPackage bundle 的 assets/game/configs/bytes/itemsoptimizecatitems{N}.bytes,
# 每条记录含"物品id(int32 小端)"与"物品名(u16 长度前缀 UTF-8)"。
ITEM_NAMES_FILE = Path(os.environ.get(
    "SEER_ITEMNAMES_OUT", _DATA_DIR / "item_names.json"))
ITEM_NAMES_STATE = Path(os.environ.get(
    "SEER_ITEMNAMES_STATE", _DATA_DIR / ".item_names_state.json"))
ITEM_ASSET_PREFIX = "assets/game/configs/bytes/itemsoptimizecatitems"
ITEM_RESOURCE_ASSET = "assets/game/configs/bytes/itemsoptimizecatitems0.bytes"  # 资源/货币类(小序号id)
# 与 itemsoptimizecatitems{0..27} 同类、也是固定"id+名字"二元表的补充物品表(中间物品/交换物品)
ITEM_EXTRA_ASSETS = (
    "assets/game/configs/bytes/midleitems.bytes",          # 中间物品(合成材料/中间产物, ~2.1 万条)
    "assets/game/configs/bytes/midleexchangeitems.bytes",  # 中间交换物品
)
# 宠物道具/药剂(主道具表 itemsoptimizecatitems3)的记录为可变长字段, id 落在该区间(见 _parse_pet_item_records)
PET_ITEM_BAND = (280000, 330000)
ITEM_ID_LO = 10000        # 标准物品 id 的下限(更小的属于资源/货币等特殊 id)

# 属性类型编号 -> 中文名 (来自 skilltypes.bytes, 单体 1-20 + 复合 21-132)
PET_ATTR_NAMES = {
    "1": "草", "2": "水", "3": "火", "4": "飞行", "5": "电", "6": "机械", "7": "地面",
    "8": "普通", "9": "冰", "10": "超能", "11": "战斗", "12": "光", "13": "暗影",
    "14": "神秘", "15": "龙", "16": "圣灵", "17": "次元", "18": "远古", "19": "邪灵",
    "20": "自然", "21": "草 超能", "22": "草 战斗", "23": "草 暗影", "24": "水 超能",
    "25": "水 暗影", "26": "水 龙", "27": "火 飞行", "28": "火 龙", "29": "火 超能",
    "30": "飞行 超能", "31": "光 飞行", "32": "飞行 龙", "33": "电 火", "34": "电 冰",
    "35": "电 战斗", "36": "暗影 电", "37": "机械 地面", "38": "机械 超能",
    "39": "机械 龙", "40": "地面 龙", "41": "战斗 地面", "42": "地面 暗影",
    "43": "冰 龙", "44": "冰 光", "45": "冰 暗影", "46": "超能 冰", "47": "战斗 火",
    "48": "战斗 暗影", "49": "光 神秘", "50": "暗影 神秘", "51": "神秘 超能",
    "52": "圣灵 光", "53": "飞行 神秘", "54": "地面 超能", "55": "暗影 龙",
    "56": "圣灵 暗影", "57": "远古 战斗", "58": "火 神秘", "59": "光 战斗",
    "60": "神秘 战斗", "61": "次元 战斗", "62": "邪灵 神秘", "63": "远古 龙",
    "64": "光 次元", "65": "远古 圣灵", "66": "水 战斗", "67": "电 龙", "68": "光 火",
    "69": "光 暗影", "70": "邪灵 龙", "71": "远古 神秘", "72": "机械 次元",
    "73": "战斗 龙", "74": "战斗 自然", "75": "邪灵 机械", "76": "电 次元",
    "77": "远古 火", "78": "圣灵 战斗", "79": "圣灵 次元", "80": "圣灵 电",
    "81": "远古 地面", "82": "远古 草", "83": "自然 龙", "84": "冰 神秘",
    "85": "飞行 暗影", "86": "冰 火", "87": "冰 飞行", "88": "自然 圣灵",
    "89": "混沌 圣灵", "90": "远古 邪灵", "91": "自然 冰", "92": "混沌 暗影",
    "93": "混沌 战斗", "94": "混沌 超能", "95": "圣灵 超能", "96": "混沌 地面",
    "97": "暗影 邪灵", "98": "混沌 远古", "99": "混沌 邪灵", "100": "圣灵 地面",
    "101": "火 暗影", "102": "光 超能", "103": "机械 战斗", "104": "飞行 电",
    "105": "混沌 飞行", "106": "混沌 龙", "107": "混沌 火", "108": "圣灵 火",
    "109": "地面 神秘", "110": "混沌 次元", "111": "混沌 冰", "112": "自然 神秘",
    "113": "虚空 邪灵", "114": "虚空 混沌", "115": "圣灵 轮回", "116": "水 次元",
    "117": "圣灵 神秘", "118": "机械 神秘", "119": "水 神秘", "120": "次元 龙",
    "121": "自然 超能", "122": "电 机械", "123": "神秘 轮回", "124": "水 机械",
    "125": "火 机械", "126": "草 机械", "127": "远古 电", "128": "圣灵 飞行",
    "129": "远古 机械", "130": "远古 光", "131": "混沌 光", "132": "火 虫",
}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
REFERER = "https://newseer.61.com"


class UpdaterError(Exception):
    pass


def log(msg):
    print(f"[头像更新] {msg}", flush=True)


def _ensure_data_dirs():
    """确保运行时数据目录存在。

    全新 ``git clone`` 时 ``data/`` 等目录本就不随仓库带来(仓库只带 app/ + 文档 + 脚本),
    若直接写 ``data/*.json`` 或头像/图标会因父目录不存在而抛 ``FileNotFoundError``,
    导致 pet_attr / skills / soulmarks 等无法生成。这里先建好目录(幂等)。
    """
    for d in (_DATA_DIR, HEAD_DIR, EFFECT_ICON_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass


def _write_json_file(path, obj):
    """写 JSON 到 path, 先确保父目录存在(全新克隆无 data/, 否则写文件抛 FileNotFoundError)."""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")


def _path_is_int_name(p):
    return bool(p.suffix.lower() == ".png" and p.stem.isdigit())


# ---------------- HTTP (stdlib) ----------------
def _http_get(url, timeout=30.0, bust=True):
    """拉取 CDN 资源; 默认加**时间戳查询参数**绕过 CDN 缓存.

    实测(2026-08): 官方 CDN 对 `PackageManifest_<Pkg>.version` 这类"内容会变但 URL 不变"的
    静态文件会返回**陈旧的缓存副本**(不加任何参数拿到 08-21/08-20, 而加时间戳参数拿到正确的
    08-28/08-29——后者才是线上游戏实际版本, 含 4937-4939 等新精灵)。给 URL 追加一个时间戳
    查询参数即强制 CDN 回源取最新。
    """
    if bust:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}_cb={int(time.time() * 1000)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REFERER})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise UpdaterError(f"HTTP {resp.status}: {url}")
            return resp.read()
    except UpdaterError:
        raise
    except Exception as e:
        raise UpdaterError(f"请求失败 {url}: {e}")


def get_remote_version():
    # 走 CDN(已加时间戳参数绕过缓存), 拿到线上游戏实际版本.
    url = f"{REMOTE_BASE}PackageManifest_{PKG}.version"
    try:
        return _http_get(url).decode("utf-8", "replace").strip()
    except UpdaterError as e:
        raise UpdaterError(f"获取版本号失败: {e}")


# ---------------- YooAsset 清单解析 ----------------
def _int(b, o):
    return struct.unpack_from("<i", b, o)[0], o + 4


def _uint(b, o):
    return struct.unpack_from("<I", b, o)[0], o + 4


def _long(b, o):
    return struct.unpack_from("<q", b, o)[0], o + 8


def _byte(b, o):
    return b[o], o + 1


def _ushort(b, o):
    return struct.unpack_from("<H", b, o)[0], o + 2


def _text(b, o):
    ln, o = _ushort(b, o)
    s = b[o:o + ln].decode("utf-8", "replace")
    return s, o + ln


def _int_list(b, o):
    n, o = _ushort(b, o)
    out = []
    for _ in range(n):
        v, o = _int(b, o)
        out.append(v)
    return out, o


def parse_manifest(data):
    """解析 YooAsset PackageManifest (FileVersion 1.5.2, newseer 变体)."""
    b = bytes(data)
    o = 0
    o += 4
    ver, o = _text(b, o)
    _, o = _byte(b, o)
    if ver > "1.4.16":
        _, o = _byte(b, o)
        _, o = _byte(b, o)
    _, o = _int(b, o)  # OutputNameType
    _, o = _text(b, o)  # PackageName
    pkg_ver, o = _text(b, o)
    ac, o = _int(b, o)
    for _ in range(ac):
        _, o = _text(b, o)
        _, o = _int(b, o)
        _, o = _int_list(b, o)
    bc, o = _int(b, o)
    bundles = []
    for _ in range(bc):
        bn, o = _text(b, o)
        if ver > "1.5.1":
            _, o = _uint(b, o)
        fh, o = _text(b, o)
        _, o = _text(b, o)
        fs, o = _long(b, o)
        _, o = _byte(b, o)
        _, o = _byte(b, o)
        _, o = _int_list(b, o)
        bundles.append((bn, fh, fs))
    return {"file_version": ver, "package_version": pkg_ver, "bundles": bundles}


def get_remote_manifest():
    version = get_remote_version()
    url = f"{REMOTE_BASE}PackageManifest_{PKG}_{version}.bytes"
    m = parse_manifest(_http_get(url))
    m["remote_version"] = version
    return m


# ---------------- 版本记录 ----------------
def load_state():
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


def is_up_to_date(remote_version):
    if not HEAD_DIR.exists():
        return False
    have_some = any(_path_is_int_name(p) for p in HEAD_DIR.iterdir() if p.is_file())
    if not have_some:
        return False
    st = load_state()
    return st.get("package") == PKG and st.get("version") == remote_version


# ---------------- 依赖自动安装 (UnityPy) ----------------
PIP_TOOL_DIR = VENDOR_DIR / "pip_tool"   # 自包含 pip (引导失败时的备选), 不污染系统
# PyPI 源: 默认用国内镜像 (快), 可用环境变量 SEER_PYPI_INDEX 覆盖; 失败时依次回退.
PYPI_INDEX = os.environ.get(
    "SEER_PYPI_INDEX", "https://pypi.tuna.tsinghua.edu.cn/simple")
PYPI_INDEX_FALLBACKS = [
    "https://mirrors.aliyun.com/pypi/simple",
    "https://pypi.org/simple",
]


def _pip_ret(pip_argv, env=None, **kw):
    """运行一次 pip 调用, 返回 subprocess.CompletedProcess."""
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(pip_argv, capture_output=True, text=True, env=e, **kw)


def _pip_available():
    """python -m pip 是否可用 (系统 or vendor/pip_tool)."""
    try:
        p = _pip_ret([sys.executable, "-m", "pip", "--version"], timeout=30)
        if p.returncode == 0:
            return True
    except Exception:
        pass
    if (PIP_TOOL_DIR / "pip").exists():
        try:
            p = _pip_ret([sys.executable, "-m", "pip", "--version"],
                         env={"PYTHONPATH": str(PIP_TOOL_DIR)}, timeout=30)
            return p.returncode == 0
        except Exception:
            pass
    return False


def _pip_env():
    """返回运行 pip 时应注入的环境 (用 vendor/pip_tool 时设 PYTHONPATH)."""
    if (PIP_TOOL_DIR / "pip").exists():
        return {"PYTHONPATH": str(PIP_TOOL_DIR)}
    return {}


def _bootstrap_pip():
    """引导 pip (应对无 pip / PEP 668 externally-managed). 返回是否成功."""
    get_pip = _PROJ / "cache" / "get-pip.py"
    get_pip.parent.mkdir(parents=True, exist_ok=True)
    url = "https://bootstrap.pypa.io/get-pip.py"
    log("需要 pip, 正在下载 get-pip.py ...")
    try:
        data = _http_get(url, timeout=120)
    except UpdaterError as e:
        log(f"下载 get-pip.py 失败: {e}")
        return False
    get_pip.write_bytes(data)
    # 依次尝试: --user -> --user --break-system-packages(PEP668) -> --target(自包含)
    attempts = [
        ["--user", "--no-warn-script-location"],
        ["--user", "--no-warn-script-location", "--break-system-packages"],
        ["--target", str(PIP_TOOL_DIR), "--no-warn-script-location"],
    ]
    for extra in attempts:
        p = _pip_ret([sys.executable, str(get_pip), *extra], timeout=300)
        if p.stdout:
            log(p.stdout.strip())
        if p.stderr:
            log("[err] " + p.stderr.strip())
        if _pip_available():
            log("pip 可用")
            return True
        log(f"尝试 {extra} 未成功, 换下一种...")
    return False


def _ensure_unitypy():
    """确保 UnityPy 可用. 优先 vendor 里的安装, 否则用 pip 装到 vendor 并加入 sys.path."""
    vendor_pkg = VENDOR_DIR / "unitypy"
    if vendor_pkg.exists():
        sys.path.insert(0, str(vendor_pkg))
    try:
        import UnityPy  # noqa: F401
        return True
    except ImportError:
        pass

    log("未检测到 UnityPy, 正在自动安装 ...")
    if not _pip_available():
        if not _bootstrap_pip():
            raise UpdaterError("无法自动安装 UnityPy: 缺少 pip 且引导安装失败")

    vendor_pkg.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "install",
           "--target", str(vendor_pkg), "--no-cache-dir",
           "--index-url", PYPI_INDEX, "UnityPy"]
    log("执行: " + " ".join(cmd))
    p = _pip_ret(cmd, env=_pip_env(), timeout=3600)
    if p.stdout:
        log(p.stdout.strip())
    if p.stderr:
        log("[err] " + p.stderr.strip())
    if p.returncode == 0:
        sys.path.insert(0, str(vendor_pkg))
        try:
            import UnityPy  # noqa: F401
            return True
        except ImportError as e:
            raise UpdaterError(f"安装完成但导入 UnityPy 失败: {e}")

    # 当前源失败 -> 依次回退到备用源重试
    log(f"使用 {PYPI_INDEX} 安装 UnityPy 失败 (exit {p.returncode}); 尝试备用源 ...")
    for idx in [i for i in PYPI_INDEX_FALLBACKS if i != PYPI_INDEX]:
        cmd = [sys.executable, "-m", "pip", "install",
               "--target", str(vendor_pkg), "--no-cache-dir",
               "--index-url", idx, "UnityPy"]
        log("执行: " + " ".join(cmd))
        p = _pip_ret(cmd, env=_pip_env(), timeout=3600)
        if p.stdout:
            log(p.stdout.strip())
        if p.stderr:
            log("[err] " + p.stderr.strip())
        if p.returncode == 0:
            sys.path.insert(0, str(vendor_pkg))
            try:
                import UnityPy  # noqa: F401
                return True
            except ImportError as e:
                raise UpdaterError(f"安装完成但导入 UnityPy 失败: {e}")
    raise UpdaterError("所有 PyPI 源均安装 UnityPy 失败")


# ---------------- 下载与解析 ----------------
def _find_pet_head_bundles(bundles):
    return [x for x in bundles if BUNDLE_GLOB in x[0].lower()]


def _find_effect_icon_bundles(bundles):
    return [x for x in bundles if EFFECT_ICON_GLOB in x[0].lower()]


def ensure_effect_icons(force=False):
    """下载 DefaultPackage 的 effecticon_*.bundle 并解出图标到 data/effecticon/.

    提供魂印/效果图标(按 icon_id 命名, 供 webui /effecticon/<id>.png). 失败不影响其它更新.
    """
    try:
        manifest = get_remote_manifest()
        bundles = _find_effect_icon_bundles(manifest["bundles"])
    except UpdaterError as e:
        log(f"effecticon 清单获取失败(跳过): {e}")
        return {"ok": False, "skipped": True, "error": str(e)}
    if not bundles:
        log("清单里没有 effecticon_* bundle, 跳过")
        return {"ok": False, "skipped": True, "error": "无 effecticon bundle"}
    try:
        _ensure_unitypy()
    except UpdaterError as e:
        log(f"effecticon 需要 UnityPy, 失败: {e}")
        return {"ok": False, "skipped": True, "error": str(e)}
    from UnityPy import Environment, enums
    types = {enums.ClassIDType.Texture2D, enums.ClassIDType.Sprite}
    EFFECT_ICON_DIR.mkdir(parents=True, exist_ok=True)
    total = 0
    for name, fh, fs in bundles:
        cache_path = CACHE_DIR / "effecticon" / (fh + ".bundle")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if not cache_path.exists() or cache_path.stat().st_size != fs:
            cache_path.write_bytes(_http_get(f"{REMOTE_BASE}{fh}"))
        try:
            env = Environment()
            env.load_file(cache_path.read_bytes(), name=cache_path.name)
            for obj in env.objects:
                try:
                    if obj.type not in types:
                        continue
                    r = obj.read()
                    img = getattr(r, "image", None)
                    if img is None:
                        continue
                    fname = None
                    for path, pptr in env.container.items():
                        if pptr.path_id == obj.path_id:
                            fname = os.path.basename(path)
                            break
                    if not fname or not fname.endswith(".png"):
                        continue
                    img.save(str(EFFECT_ICON_DIR / fname))
                    total += 1
                except Exception:
                    pass
        except Exception as e:
            log(f"解析 {name} 失败: {e}")
    log(f"effecticon 已更新: {total} 个图标 -> {EFFECT_ICON_DIR}")
    return {"ok": True, "count": total, "skipped": False}


def _download_bundle(name, fhash, fsize):
    cache_path = CACHE_DIR / (fhash + ".bundle")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.exists() and cache_path.stat().st_size == fsize:
        return False
    url = f"{REMOTE_BASE}{fhash}"
    data = _http_get(url)
    cache_path.write_bytes(data)
    if len(data) != fsize:
        log(f"警告: {name} 下载大小 {len(data)} != 清单 {fsize}")
    log(f"已下载 {name} (hash={fhash[:8]}, {len(data)}B)")
    return True


def _extract_id(text):
    if not text:
        return None
    import re
    nums = re.findall(r"(\d+)", text)
    return int(nums[-1]) if nums else None


def _asset_name(r):
    """取一个 Unity 对象的名字: Sprite/Texture 的 id 常放在 m_Name, 而非 name."""
    return getattr(r, "m_Name", "") or getattr(r, "name", "") or ""


def extract_pet_avatars(cache_dir, out_dir):
    """用 UnityPy 把 cache_dir 里的 pet_head_*.bundle 解析成 out_dir/<id>.png.
    返回导出的图片数量."""
    from UnityPy import Environment, enums

    cache_dir = Path(cache_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    env = Environment()
    bundles = sorted(cache_dir.glob("*.bundle"))
    if not bundles:
        log(f"{cache_dir} 下没有 *.bundle, 无法解析")
        return 0
    for p in bundles:
        try:
            env.load_file(p.read_bytes(), name=p.name)
        except Exception as e:
            log(f"载入失败 {p.name}: {e}")
    types = {enums.ClassIDType.Texture2D, enums.ClassIDType.Sprite}
    candidates = []
    # 1) container 主资源 (带地址路径)
    try:
        items = list(env.container.items())
    except AttributeError:
        items = []
    for path, pptr in items:
        try:
            obj = pptr.deref() if hasattr(pptr, "deref") else pptr
            if obj.type not in types:
                continue
            r = obj.read()
            img = getattr(r, "image", None)
            if img is None:
                continue
            nm = _asset_name(r)
            pid = _extract_id(str(path)) or _extract_id(nm)
            candidates.append((pid, str(path), nm, img))
        except Exception as e:
            log(f"container 处理失败 {path}: {e}")
    # 2) 遍历全部对象 (子资源可能不在 container, 只有 m_Name 带 id)
    for obj in env.objects:
        try:
            if obj.type not in types:
                continue
            r = obj.read()
            img = getattr(r, "image", None)
            if img is None:
                continue
            nm = _asset_name(r)
            pid = _extract_id(nm) or _extract_id(str(obj.path))
            candidates.append((pid, str(getattr(obj, "path", "")), nm, img))
        except Exception as e:
            log(f"对象处理失败: {e}")

    written = 0
    seen = set()
    skipped = 0
    for pid, path, name, img in candidates:
        if pid is None:
            # 该项多为非宠物/无法定位 id 的资源; 逐条刷屏意义不大, 仅计数
            skipped += 1
            continue
        if pid in seen:
            continue
        seen.add(pid)
        try:
            if getattr(img, "mode", None) and img.mode.lower() not in ("rgba", "rgb"):
                img = img.convert("RGBA")
            img.save(str(out_dir / f"{pid}.png"))
            written += 1
        except Exception as e:
            log(f"[失败] id={pid} path={path!r}: {e}")
    log(f"解析导出完成: {written} 张 -> {out_dir}" + (f" (另跳过 {skipped} 个无法定位 id 的资源)" if skipped else ""))
    return written


def _run_extractor():
    """执行解析: 若用户自定义了提取命令则用它, 否则进程内用 UnityPy 解析."""
    if os.environ.get("SEER_AVATAR_EXTRACTOR"):
        cmd = shlex.split(os.environ["SEER_AVATAR_EXTRACTOR"])
        log("调用自定义提取命令: " + " ".join(cmd))
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if p.stdout:
            log(p.stdout.strip())
        if p.stderr:
            log("[err] " + p.stderr.strip())
        if p.returncode != 0:
            raise UpdaterError(f"自定义提取命令退出码 {p.returncode}")
        return
    # 默认: 进程内解析 (UnityPy 已由 _ensure_unitypy 确保可用)
    _ensure_unitypy()
    n = extract_pet_avatars(CACHE_DIR, HEAD_DIR)
    if n == 0:
        raise UpdaterError("解析完成但未导出任何 <id>.png")


# ---------------- petbook (精灵图鉴名字) ----------------
def _read_json(path):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _avatar_ids():
    """data/head 下已有头像的物种 id 集合."""
    import glob as _g
    out = set()
    for f in _g.glob(str(HEAD_DIR / "*.png")):
        stem = os.path.basename(f)[:-4]
        if stem.isdigit():
            out.add(int(stem))
    return out


_PETBOOK_ANCHOR = "布布种子"   # 图鉴首只精灵 (id=1), 用于定位 monster 数组起点

def extract_petbook_names(pb):
    """从 petbook.bytes 的 monster 数组还原 {物种id: 名字}.

    结构(经真实数据解码验证): monster 是连续定长记录流, 每条记录为
        def_name  = u16小端长度 + UTF-8 字符串
        features  = u16小端长度 + UTF-8 字符串      (图鉴描述)
        id        = u32 小端
        target    = u16小端长度 + UTF-8 字符串
    记录按顺序紧排, 首条 id=1 布布种子, 末条 ≈ 拿瓦铠甲(id 4936), 共 4901 条。
    返回 {int 物种id: str 名字}。
    """
    anchor = _PETBOOK_ANCHOR.encode("utf-8")
    o = pb.find(struct.pack("<H", len(anchor)) + anchor)
    if o == -1:
        return {}

    def u16(p):
        return struct.unpack_from("<H", pb, p)[0], p + 2

    def u32(p):
        return struct.unpack_from("<I", pb, p)[0], p + 4

    def txt(p):
        L, p = u16(p)
        return pb[p:p + L].decode("utf-8", "replace"), p + L

    names = {}
    # monster id <= 5000 (图鉴物种 id 上限), rec_mintmark 记录会越界或带 app 跳转
    for _ in range(8000):
        if o + 2 > len(pb):
            break
        try:
            dn, o = txt(o)
            fe, o = txt(o)
            rid, o = u32(o)
            tg, o = txt(o)
        except (struct.error, ValueError):
            break
        # 判据: 越过 monster 数组(进入 rec_mintmark)即停止
        if not dn or not (1 <= rid <= 5000):
            break
        if "app/" in fe or "app/" in tg or ("map" in fe and "module" in fe):
            break
        names[rid] = dn
    return names


def _get_asset_bytes(bundle_path, asset):
    """从 ConfigPackage bundle 里取出指定资源(如 petbook.bytes/monsters.bytes)的原始字节."""
    from UnityPy import Environment
    env = Environment()
    env.load_file(Path(bundle_path).read_bytes(), name=Path(bundle_path).name)
    for path, pptr in env.container.items():
        if path.lower() == asset:
            s = pptr.deref().read().m_Script
            return s.encode("utf-8", "surrogateescape") if isinstance(s, str) else bytes(s)
    raise UpdaterError(f"bundle 里没有 {asset}")


def _get_petbook_bytes(bundle_path):
    return _get_asset_bytes(bundle_path, PETBOOK_ASSET)


def ensure_petbook(force=False):
    """确保 petbook.json 与 ConfigPackage 版本同步 (下载+解析图鉴名字).

    返回结果里带 cache_path (ConfigPackage bundle 的本地缓存路径),
    便于调用方从中再取 monsters.bytes 等其它资源.
    """
    _ensure_data_dirs()
    # 走 CDN(已加时间戳参数绕过缓存), 拿到线上游戏实际版本
    try:
        cfg_version = _http_get(
            f"{CONFIG_BASE}PackageManifest_{CONFIG_PKG}.version").decode().strip()
    except UpdaterError as e:
        log(f"获取图鉴包版本失败: {e}")
        return {"ok": False, "skipped": True, "error": str(e)}
    cache_path = None
    try:
        _ensure_unitypy()
        mf = parse_manifest(_http_get(
            f"{CONFIG_BASE}PackageManifest_{CONFIG_PKG}_{cfg_version}.bytes"))
        if not mf["bundles"]:
            return {"ok": False, "skipped": True, "version": cfg_version,
                    "error": "图鉴包无 bundle", "cache_path": None}
        bn, fh, fs = mf["bundles"][0]
        cache_path = CONFIG_CACHE / (fh + ".bundle")
        if not cache_path.exists() or cache_path.stat().st_size != fs:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(_http_get(f"{CONFIG_BASE}{fh}"))
            log(f"已下载图鉴包 bundle ({bn}, {fs}B)")
        st = _read_json(PETBOOK_STATE)
        if not force and st.get("version") == cfg_version and PETBOOK_FILE.exists():
            log(f"petbook 已是最新 (版本 {cfg_version}), 跳过")
            return {"ok": True, "skipped": True, "version": cfg_version,
                    "error": None, "cache_path": str(cache_path)}
        pb = _get_petbook_bytes(cache_path)
        names = extract_petbook_names(pb)
        if not names:
            return {"ok": False, "skipped": True, "version": cfg_version,
                    "error": "从 petbook.bytes 解析不到名字", "cache_path": None}
        _write_json_file(PETBOOK_FILE, {str(k): v for k, v in names.items()})
        _write_json_file(PETBOOK_STATE, {"version": cfg_version,
                                        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        log(f"petbook 已更新: {len(names)} 个精灵名 -> {PETBOOK_FILE}")
        return {"ok": True, "skipped": False, "version": cfg_version,
                "error": None, "cache_path": str(cache_path)}
    except UpdaterError as e:
        log(f"petbook 更新失败: {e}")
        return {"ok": False, "skipped": True, "version": cfg_version,
                "error": str(e),
                "cache_path": str(cache_path) if cache_path else None}


def parse_monsters(pb):
    """解析 monsters.bytes -> 精灵记录列表.

    移植自 Sunny 赛尔号数据解析 (Solaris monsters.py):
    每条记录的各段 (extra_moves/learnable_moves/move/show_extra_moves/sp_extra_moves)
    均为可选, 前面各带 1 字节布尔开关, 必须严格按 C# 字段顺序读取, 否则会错位.
    """
    o = [0]

    def _boo():
        v = pb[o[0]] != 0
        o[0] += 1
        return v

    def _i32():
        v = struct.unpack_from("<i", pb, o[0])[0]
        o[0] += 4
        return v

    def _u16():
        v = struct.unpack_from("<H", pb, o[0])[0]
        o[0] += 2
        return v

    def _txt():
        L = _u16()
        v = pb[o[0]:o[0] + L].decode("utf-8", "replace")
        o[0] += L
        return v

    def _learnable():
        adv, mov, sp = [], [], []
        if _boo():                       # 先 Boolean, 再 i32 条数
            n = _i32()
            for _ in range(n):
                adv.append({"id": _i32(), "learning_lv": _i32(), "rec": _i32(),
                            "tag": _i32(), "tag2": _i32()})
        if _boo():
            n = _i32()
            for _ in range(n):
                mov.append({"id": _i32(), "learning_lv": _i32(), "rec": _i32(),
                            "tag": _i32()})
        if _boo():
            n = _i32()
            for _ in range(n):
                sp.append({"id": _i32(), "learning_lv": _i32(), "rec": _i32(),
                           "tag": _i32(), "tag2": _i32()})
        return {"adv_move": adv, "move": mov, "sp_move": sp}

    if not _boo():                       # 文件头: 是否有精灵数据
        return []
    monsters = []
    if _boo():                           # 文件头: 是否有精灵容器
        count = _i32()
        for _ in range(count):
            atk = _i32(); character_attr_param = _i32(); combo = _i32(); def_ = _i32()
            def_name = _txt()
            evolv_flag = _i32(); evolves_to = _i32(); evolving_lv = _i32()
            extra_moves = _learnable() if _boo() else None
            free_forbidden = _i32(); gender = _i32(); hp = _i32(); mid = _i32()
            learnable_moves = _learnable() if _boo() else None
            move = None
            if _boo():
                move = {"id": _i32(), "learning_lv": _i32(), "rec": _i32(), "tag": _i32()}
            pet_class = _i32(); real_id = _i32()
            show_extra_moves = _learnable() if _boo() else None
            sp_atk = _i32(); sp_def = _i32()
            sp_extra_moves = _learnable() if _boo() else None
            spd = _i32(); support = _i32(); transform = _i32(); typev = _i32()
            vip = _i32(); is_fly_pet = _i32(); is_ride_pet = _i32()
            monsters.append({
                "id": mid, "real_id": real_id, "type": typev, "atk": atk, "hp": hp,
                "def_name": def_name, "learnable_moves": learnable_moves,
                "extra_moves": extra_moves, "move": move,
                "show_extra_moves": show_extra_moves, "sp_extra_moves": sp_extra_moves,
                "sp_atk": sp_atk, "sp_def": sp_def, "spd": spd,
                "support": support, "transform": transform, "vip": vip,
                "is_fly_pet": is_fly_pet, "is_ride_pet": is_ride_pet,
                "pet_class": pet_class,
            })
    return monsters


def parse_skill_types(pb):
    """解析 skilltypes.bytes -> {type id(str): 中文名}.

    移植自 Solaris skilltype.py: 每条为 att(cn的组合id串,可选)|cn(中文名)|
    en(可选英文名列表)|id|is_dou。属性名与怪物 type 编号都来自游戏资源本身, 可自更新.
    """
    o = [0]

    def _boo():
        v = pb[o[0]] != 0
        o[0] += 1
        return v

    def _i32():
        v = struct.unpack_from("<i", pb, o[0])[0]
        o[0] += 4
        return v

    def _u16():
        v = struct.unpack_from("<H", pb, o[0])[0]
        o[0] += 2
        return v

    def _txt():
        L = _u16()
        v = pb[o[0]:o[0] + L].decode("utf-8", "replace")
        o[0] += L
        return v

    if not (_boo() and _boo()):          # 文件头两个 bool
        return {}
    num = _i32()
    out = {}
    for _ in range(num):
        att = _txt()
        cn = _txt()
        if _boo():                        # en 列表(可选)
            n = _i32()
            for _ in range(n):
                _txt()
        tid = _i32(); _i32()              # id, is_dou
        if cn:
            out[str(tid)] = cn
    return out


def parse_moves(pb):
    """解析 moves.bytes -> {技能id: 技能基础数据}.

    移植自 Solaris moves.py(MovesParser): 数组前的各可选段(friend_side_effect/arg,
    side_effect/arg)均为 [布尔][i32条数][i32...], 必须按 C# 字段顺序读取.
    """
    o = [0]

    def _boo():
        v = pb[o[0]] != 0
        o[0] += 1
        return v

    def _i32():
        v = struct.unpack_from("<i", pb, o[0])[0]
        o[0] += 4
        return v

    def _u16():
        v = struct.unpack_from("<H", pb, o[0])[0]
        o[0] += 2
        return v

    def _txt():
        L = _u16()
        v = pb[o[0]:o[0] + L].decode("utf-8", "replace")
        o[0] += L
        return v

    def _int_list():
        if not _boo():
            return []
        n = _i32()
        return [_i32() for _ in range(n)]

    if not _boo():                      # MovesTbl
        return {}
    if not _boo():                      # Moves
        return {}
    if not _boo():                      # Move 数组
        return {}
    count = _i32()
    out = {}
    for _ in range(count):
        accuracy = _i32(); _i32(); _i32(); _i32(); crit_rate = _i32()
        _int_list(); _int_list()        # friend_side_effect / arg
        mid = _i32(); max_pp = _i32(); _i32(); must_hit = _i32()
        name = _txt(); power = _i32(); priority = _i32()
        side_effect = _int_list()
        side_effect_arg = _int_list()
        ty = _i32(); info = _txt(); _i32()
        out[str(mid)] = {
            "name": name, "pp": max_pp, "type": ty, "power": power,
            "accuracy": accuracy, "crit": crit_rate, "mustHit": must_hit,
            "priority": priority, "side_effect": side_effect,
            "side_effect_arg": side_effect_arg, "info": info,
        }
    return out


def parse_skill_effects(pb):
    """解析 skill_effect.bytes -> {效果id: {info 描述模板, argsNum 参数个数, tagA/tagB/tagC}}.

    移植自 Solaris skill_effect.py(SkillEffectParser): 每字段按 C# ISkillEffectInfo.Parse 顺序读取.
    """
    o = [0]

    def _boo():
        v = pb[o[0]] != 0
        o[0] += 1
        return v

    def _i32():
        v = struct.unpack_from("<i", pb, o[0])[0]
        o[0] += 4
        return v

    def _u16():
        v = struct.unpack_from("<H", pb, o[0])[0]
        o[0] += 2
        return v

    def _txt():
        L = _u16()
        v = pb[o[0]:o[0] + L].decode("utf-8", "replace")
        o[0] += L
        return v

    if not _boo():
        return {}
    count = _i32()
    out = {}
    for _ in range(count):
        _i32()                          # Bosseffective
        args_num = _i32()
        _txt()                          # formattingAdjustment
        eid = _i32()
        _txt()                          # ifTextItalic
        info = _txt()
        _i32()                          # isif
        tag_a = _txt(); _i32()          # tagA, tagAboss
        tag_b = _txt(); _i32()          # tagB, tagBboss
        tag_c = _txt(); _i32()          # tagC, tagCboss
        out[str(eid)] = {
            "info": info, "argsNum": args_num,
            "tagA": tag_a, "tagB": tag_b, "tagC": tag_c,
        }
    return out


def _format_effect_desc(template, args):
    """把 skill_effect 的描述模板里的 {0}/{1}/{2} 用效果参数替换成可读文本."""
    if not template:
        return ""
    try:
        return template.format(*args) if args else template
    except (IndexError, ValueError, KeyError):
        return template


def regenerate_skills(moves_pb, effects_pb, type_names=None):
    """由 moves.bytes + skill_effect.bytes + 属性名表, 生成 skills.json: {技能id: 技能数据}.

    技能数据含: name/pp/type/typeName/power/accuracy/crit/mustHit/priority,
    以及 effects(每个效果: id/args/desc 描述)。数据完全来自游戏资源本身, 可自更新.
    失败时返回 {"ok": False, "error": ...}, 不影响其它更新.
    """
    _ensure_data_dirs()
    try:
        moves = parse_moves(moves_pb) if moves_pb else {}
        effects = parse_skill_effects(effects_pb) if effects_pb else {}
    except Exception as e:
        return {"ok": False, "error": f"解析技能数据失败: {e}"}
    if not moves:
        return {"ok": False, "error": "moves.bytes 解析结果为空"}
    names = type_names or PET_ATTR_NAMES
    out = {}
    for sid, m in moves.items():
        se = m.get("side_effect") or []
        sea = m.get("side_effect_arg") or []
        effs = []
        sea_idx = 0
        for eid in se:
            ifront = effects.get(str(eid), {})
            # 每个效果按 argsNum 从扁平 side_effect_arg 中消费对应数量的参数
            an = ifront.get("argsNum", 0) or 0
            args = sea[sea_idx:sea_idx + an] if an > 0 else []
            sea_idx += an
            effs.append({
                "id": eid,
                "args": args,
                "desc": _format_effect_desc(ifront.get("info"), args),
                "tag": ifront.get("tagA", ""),
            })
        out[str(sid)] = {
            "name": m.get("name", ""),
            "pp": m.get("pp", 0),
            "type": m.get("type", 0),
            "typeName": names.get(str(m.get("type")), "(%s)" % m.get("type")),
            "power": m.get("power", 0),
            "accuracy": m.get("accuracy", 0),
            "crit": m.get("crit", 0),
            "mustHit": m.get("mustHit", 0),
            "priority": m.get("priority", 0),
            "effects": effs,
            "info": m.get("info", ""),
        }
    if not out:
        return {"ok": False, "error": "技能数据为空"}
    _write_json_file(SKILLS_FILE, out)
    log(f"skills 已生成: {len(out)} 个技能 -> {SKILLS_FILE}")
    return {"ok": True, "count": len(out)}


def parse_effect_tags(pb):
    """解析 effectag.bytes -> {标签id: 标签名}. 移植自 Solaris effect_tag.py."""
    o = [0]

    def _boo():
        v = pb[o[0]] != 0
        o[0] += 1
        return v

    def _i32():
        v = struct.unpack_from("<i", pb, o[0])[0]
        o[0] += 4
        return v

    def _u16():
        v = struct.unpack_from("<H", pb, o[0])[0]
        o[0] += 2
        return v

    def _txt():
        L = _u16()
        v = pb[o[0]:o[0] + L].decode("utf-8", "replace")
        o[0] += L
        return v

    out = {}
    if not _boo():
        return out
    count = _i32()
    for _ in range(count):
        tid = _i32()
        tag = _txt()
        out[str(tid)] = tag
    return out


def parse_effect_icons(pb):
    """解析 effecticon.bytes -> {图标id: {id,tips,analyze,effect_id,args,kind,tag,pet_id}}.

    魂印(专属特性)即 effectIcon 中带 pet_id(拥有该魂印的精灵)的条目.
    移植自 Solaris effect_icon.py(EffectIconParser).
    """
    o = [0]

    def _boo():
        v = pb[o[0]] != 0
        o[0] += 1
        return v

    def _i32():
        v = struct.unpack_from("<i", pb, o[0])[0]
        o[0] += 4
        return v

    def _u16():
        v = struct.unpack_from("<H", pb, o[0])[0]
        o[0] += 2
        return v

    def _txt():
        L = _u16()
        v = pb[o[0]:o[0] + L].decode("utf-8", "replace")
        o[0] += L
        return v

    def _int_list():
        if not _boo():
            return []
        n = _i32()
        return [_i32() for _ in range(n)]

    def _str_list():
        if not _boo():
            return []
        n = _i32()
        return [_txt() for _ in range(n)]

    if not _boo():
        return {}
    if not _boo():
        return {}
    count = _i32()
    out = {}
    for _ in range(count):
        item_id = _i32()
        analyze = _txt(); args = _txt(); come = _txt()
        _str_list()                     # des
        effect_id = _i32()
        icon_id = _i32()
        _i32()                          # intensify
        _i32()                          # is_adv
        kind = _int_list()
        _i32()                          # label
        _i32()                          # limited_type
        pet_id = _int_list()
        _int_list()                     # specific_id
        tag = _str_list()
        _i32()                          # target
        tips = _txt()
        _i32()                          # to
        _i32()                          # type
        out[str(item_id)] = {
            "id": item_id, "tips": tips, "analyze": analyze,
            "effect_id": effect_id, "args": args, "kind": kind,
            "tag": tag, "pet_id": pet_id, "icon_id": icon_id,
        }
    return out


def regenerate_soulmarks(icon_pb, tag_pb=None):
    """由 effecticon.bytes + effectag.bytes 生成 soulmarks.json: {精灵id: [魂印数据]}.

    魂印(专属特性) = effectIcon 中 pet_id 包含该精灵的条目; 每条含 tags(标签名列表),
    desc(tips), analyze, effect_id, args。失败时返回 {"ok": False, ...}。
    """
    _ensure_data_dirs()
    try:
        icons = parse_effect_icons(icon_pb) if icon_pb else {}
        tags = parse_effect_tags(tag_pb) if tag_pb else {}
    except Exception as e:
        return {"ok": False, "error": f"解析魂印数据失败: {e}"}
    out = {}
    for sid, s in icons.items():
        pet_ids = s.get("pet_id") or []
        if not pet_ids:
            continue
        # 标签: effecticon 的 kind 是 0 基, 对应 effectag 的 tag id = kind + 1 (对齐 Solaris SoulmarkAnalyzer)
        tag_names = [tags.get(str(k + 1), "(%s)" % (k + 1)) for k in (s.get("kind") or [])]
        entry = {
            "id": s.get("id"), "tags": tag_names, "desc": s.get("tips", ""),
            "analyze": s.get("analyze", ""), "effectId": s.get("effect_id"),
            "args": s.get("args", ""), "iconId": s.get("icon_id"),
        }
        for p in pet_ids:
            out.setdefault(str(p), []).append(entry)
    if not out:
        return {"ok": False, "error": "魂印数据为空"}
    _write_json_file(SOULMARKS_FILE, out)
    log(f"soulmarks 已生成: {len(out)} 个精灵带魂印 -> {SOULMARKS_FILE}")
    return {"ok": True, "count": len(out)}


# ---------------- 物品名 (itemsoptimizecatitems{N}.bytes) ----------------
def _list_asset_bytes(bundle_path, prefix=None, extra_assets=()):
    """从 bundle 里取出所有路径以 prefix 开头的资产, 及 extra_assets 指定的具体资产: {asset_path: bytes}."""
    from UnityPy import Environment
    env = Environment()
    env.load_file(Path(bundle_path).read_bytes(), name=Path(bundle_path).name)
    out = {}
    for path, pptr in env.container.items():
        if (prefix and path.lower().startswith(prefix)) or path in extra_assets:
            s = pptr.deref().read().m_Script
            data = s.encode("utf-8", "surrogateescape") if isinstance(s, str) else bytes(s)
            out[path] = data
    return out


def _item_names_and_prefixes(data):
    """返回 [(name_len_prefix_offset, name_str)], 名字以汉字开头且含>=2 个汉字."""
    out = []
    for i in range(len(data) - 2):
        ln = struct.unpack_from("<H", data, i)[0]
        if not (2 <= ln <= 40) or i + 2 + ln > len(data):
            continue
        try:
            s = data[i + 2:i + 2 + ln].decode("utf-8")
        except UnicodeDecodeError:
            continue
        if len(ITEM_NAME_CJK.findall(s)) >= 2 and "\u4e00" <= s[0] <= "\u9fff":
            out.append((i, s))
    return out


def _item_id_ok(v):
    """标准物品 id 的保守区间."""
    return ITEM_ID_LO <= v <= 20000000


def _item_best_offset(data, anchors):
    """在候选 K(4..40 步长4)里选"干净"的: 命中数最多, 且命中 id 不完全相同."""
    best = None
    for k in range(4, 41, 4):
        vals = []
        for i, _ in anchors:
            o = i - k
            if o >= 0 and o + 4 <= len(data):
                v = struct.unpack_from("<I", data, o)[0]
                if _item_id_ok(v):
                    vals.append(v)
        if not vals:
            continue
        # 退化: 命中过多但 id 几乎全同(多为数量/常量字段), 跳过
        if len(set(vals)) <= max(1, len(vals) // 10):
            continue
        cand = (len(vals), k)
        if best is None or cand > best:
            best = cand
    return best


def _parse_pet_item_records(data):
    """按 [id][u32][u32][u16 前缀][前缀][i32][u16 名][名] 布局解析宠物道具带内记录.

    宠物道具/药剂(itemsoptimizecatitems3)的记录为可变长字段, id 落在 PET_ITEM_BAND 且
    可能出现在任意字节位置(记录含变长串, 边界不按 4 字节对齐)。逐字节定位 u32 并向前解出,
    校验(前缀/名字长度合法、名字为汉字)后返回 [(id, name)]。
    """
    lo, hi = PET_ITEM_BAND
    out = []
    seen = set()
    n = len(data)
    i = 0
    while i + 4 <= n:
        v = struct.unpack_from("<I", data, i)[0]
        if not (lo <= v <= hi) or v % 65536 == 0 or (v & (v - 1)) == 0:
            i += 1
            continue
        try:
            p = i + 4 + 4 + 4            # id + x1 + x2
            plen = struct.unpack_from("<H", data, p)[0]
            p += 2
            if not (0 <= plen <= 12) or p + plen > n:
                i += 1
                continue
            p += plen                    # 前缀串(如 "1895")
            p += 4                        # i32
            nlen = struct.unpack_from("<H", data, p)[0]
            p += 2
            if not (1 <= nlen <= 40) or p + nlen > n:
                i += 1
                continue
            name = data[p:p + nlen].decode("utf-8", "replace")
            if len(ITEM_NAME_CJK.findall(name)) >= 2 and "\u4e00" <= name[0] <= "\u9fff":
                if v not in seen:
                    seen.add(v)
                    out.append((v, name))
        except (struct.error, UnicodeDecodeError):
            pass
        i += 1
    return out


def _extract_item_pairs(data):
    """对一个 itemsoptimizecatitems{N} 文件提取 [(id, name)].

    多数类用"固定 K"对齐(id 恰在名字长度前缀前 K 字节, ~100% 命中); 匹配率过低则回退到
    宠物道具可变长记录解析(_parse_pet_item_records)。
    """
    anchors = _item_names_and_prefixes(data)
    if not anchors:
        return None
    best = _item_best_offset(data, anchors)
    if best is not None:
        count, k = best
        if count >= len(anchors) * 0.6:
            pairs = []
            seen = set()
            for i, s in anchors:
                o = i - k
                if o >= 0 and o + 4 <= len(data):
                    v = struct.unpack_from("<I", data, o)[0]
                    if _item_id_ok(v) and v not in seen:
                        seen.add(v)
                        pairs.append((v, s))
            return {"k": k, "count": len(pairs), "pairs": pairs}
    pet_pairs = _parse_pet_item_records(data)
    if pet_pairs:
        return {"k": "petitem", "count": len(pet_pairs), "pairs": pet_pairs}
    return None


def _extract_resource_names(data):
    """解析资源/货币类(itemsoptimizecatitems0): 每项 id 为资源小序号(名字长度前缀前 12 字节).

    返回 {资源id(小整数): 资源名}。这类资源(赛尔豆/钻石/燃料...)是独立的资源/货币系统,
    其 id 为 1..15 的小序号, 与标准物品 id(≥10000)不冲突。
    """
    anchors = _item_names_and_prefixes(data)
    out = {}
    for i, s in anchors:
        o = i - 12
        if o >= 0 and o + 4 <= len(data):
            rid = struct.unpack_from("<I", data, o)[0]
            if 1 <= rid <= 100:
                out[rid] = s
    return out


def regenerate_item_names(bundle_path):
    """从 ConfigPackage bundle 解析全部物品名, 生成 data/item_names.json: {物品id: 物品名}.

    覆盖:
      - itemsoptimizecatitems{N} 各物品大类(固定 K 对齐) + 宠物道具(可变长记录解析)
      - midleitems / midleexchangeitems(中间物品/交换物品, 同为"id+名字"二元表)
      - 资源/货币(itemsoptimizecatitems0, id 为 1..15 小序号)
    失败时返回 {"ok": False, ...}。
    """
    _ensure_data_dirs()
    try:
        assets = _list_asset_bytes(bundle_path, ITEM_ASSET_PREFIX, extra_assets=ITEM_EXTRA_ASSETS)
    except Exception as e:
        return {"ok": False, "error": f"枚举物品资产失败: {e}"}
    if not assets:
        return {"ok": False, "error": "bundle 里没有 itemsoptimizecatitems* 资产"}
    merged = {}
    detail = {}

    def _absorb(asset, data):
        name = Path(asset).name
        if asset == ITEM_RESOURCE_ASSET:
            rnames = _extract_resource_names(data)
            for rid, nm in rnames.items():
                merged.setdefault(rid, nm)
            detail[name] = {"n": len(rnames), "k": "resource"}
            return
        res = _extract_item_pairs(data)
        if res is None:
            detail[name] = {"n": 0, "k": "skip"}
            return
        for v, s in res["pairs"]:
            merged.setdefault(v, s)
        detail[name] = {"n": len(res["pairs"]), "k": res["k"]}

    for asset, data in assets.items():
        _absorb(asset, data)
    if not merged:
        return {"ok": False, "error": "未解析到任何物品名"}
    _write_json_file(ITEM_NAMES_FILE, {str(k): v for k, v in merged.items()})
    log(f"item_names 已生成: {len(merged)} 个物品名 -> {ITEM_NAMES_FILE}")
    return {"ok": True, "count": len(merged), "detail": detail}


def regenerate_pet_attr_from_bytes(pb, attr_names=None):
    """直接从 monsters.bytes 自解析生成 pet_attr.json: {物种id: 属性名}.

    只取 real_id==0 的基表记录: id 即物种编号, type 即属性类型编号,
    type 经 attr_names(默认用内置 PET_ATTR_NAMES, 可由 skilltypes.bytes 自解析结果覆盖)
    转为中文。数据完全来源于游戏资源本身, 可随版本自更新.
    """
    _ensure_data_dirs()
    if not pb:
        return {"ok": False, "error": "monsters.bytes 为空"}
    try:
        monsters = parse_monsters(pb)
    except Exception as e:
        return {"ok": False, "error": f"解析 monsters.bytes 失败: {e}"}
    if not monsters:
        return {"ok": False, "error": "monsters.bytes 解析结果为空"}
    names = attr_names or PET_ATTR_NAMES
    attr = {}
    for r in monsters:
        if r.get("real_id") != 0:
            continue
        sid, ty = r.get("id"), r.get("type")
        if not isinstance(sid, int) or not isinstance(ty, int) or ty <= 0:
            continue
        attr[str(sid)] = names.get(str(ty), "(%s)" % ty)
    if not attr:
        return {"ok": False, "error": "从 monsters.bytes 未解析到属性"}
    _write_json_file(PET_ATTR_FILE, attr)
    log(f"pet_attr 已从 monsters.bytes 生成: {len(attr)} 个精灵属性 -> {PET_ATTR_FILE}")
    return {"ok": True, "count": len(attr)}


def regenerate_pet_names_from_bytes(pb):
    """从 monsters.bytes 生成**全物种名兜底表** monster_names.json: {物种id: def_name}.

    与 petbook.json 的区别:
      - petbook.json(图鉴表) 只覆盖 id <= 5000 的 4901 个物种, 名字更"正式";
      - monsters.bytes 覆盖**全部**物种(含 id > 5000 的 1500+ 个), 每条带 def_name。
    两者互补: 界面优先用图鉴名, 图鉴没有的再用本表兜底, 避免出现 (id=5357) 这样的占位。

    仅收 real_id==0 的基表记录(与 pet_attr 同一口径), 跳过空名。
    """
    _ensure_data_dirs()
    if not pb:
        return {"ok": False, "error": "monsters.bytes 为空"}
    try:
        monsters = parse_monsters(pb)
    except Exception as e:
        return {"ok": False, "error": f"解析 monsters.bytes 失败: {e}"}
    names = {}
    for r in monsters:
        if r.get("real_id") != 0:
            continue
        sid = r.get("id")
        nm = (r.get("def_name") or "").strip()
        if isinstance(sid, int) and nm:
            names[str(sid)] = nm
    if not names:
        return {"ok": False, "error": "从 monsters.bytes 未解析到精灵名"}
    _write_json_file(MONSTER_NAMES_FILE, names)
    log(f"monster_names 已从 monsters.bytes 生成: {len(names)} 个精灵名 -> {MONSTER_NAMES_FILE}")
    return {"ok": True, "count": len(names)}


def regenerate_pet_attr(monsters_json_path=MONSTERS_JSON):
    """从 refs/monsters.json (别人解析好的 monsters.bytes) 生成 pet_attr.json: {物种id: 属性名}.

    monsters.json 结构为 {"monsters": {"monster": [ {def_name,type,id,real_id,...}, ... ]}}。
    规则: real_id==0 的基表记录其 id 即物种编号, type 即属性类型编号 (属性在 type 参数上),
    type 经 PET_ATTR_NAMES 转为中文 (如水/火/龙/水 龙)。失败不影响其它更新。
    """
    _ensure_data_dirs()
    try:
        data = json.loads(Path(monsters_json_path).read_text("utf-8"))
    except (OSError, ValueError) as e:
        return {"ok": False, "error": str(e)}
    monsters = data.get("monsters")
    if isinstance(monsters, dict):
        arr = monsters.get("monster")
        if not isinstance(arr, list):
            arr = next(iter(monsters.values()), [])
    else:
        arr = monsters
    if not isinstance(arr, list):
        return {"ok": False, "error": "monsters 结构异常"}
    attr = {}
    for r in arr:
        if not isinstance(r, dict):
            continue
        if r.get("real_id") != 0:            # 只取物种本体(基表记录), 跳过皮肤/变体
            continue
        sid, ty = r.get("id"), r.get("type")
        if not isinstance(sid, int) or not isinstance(ty, int) or ty <= 0:
            continue
        attr[str(sid)] = PET_ATTR_NAMES.get(str(ty), "(%s)" % ty)
    if not attr:
        return {"ok": False, "error": "未解析到属性"}
    _write_json_file(PET_ATTR_FILE, attr)
    log(f"pet_attr 已生成: {len(attr)} 个精灵属性 -> {PET_ATTR_FILE}")
    return {"ok": True, "count": len(attr)}


# ---------------- 主流程 ----------------
def ensure_pet_avatars(force=False):
    """启动时调用: 检查并更新全部精灵头像 + 图鉴名字(各自随版本独立刷新)."""
    _ensure_data_dirs()   # 全新克隆没有 data/, 先建好, 避免写 json/图标失败
    try:
        remote_version = get_remote_version()
    except UpdaterError as e:
        log(f"无法获取远端版本, 跳过本次更新: {e}")
        return {"ok": False, "skipped": True, "version": None, "error": str(e)}

    # 图鉴名字随其 ConfigPackage 版本独立刷新(失败不影响头像/服务启动, 与头像同理念)
    pb_result = {}
    try:
        pb_result = ensure_petbook(force=force)
    except Exception as e:
        log(f"petbook 更新未完成: {e}")

    # 精灵属性表 (monsters.bytes 的 type) + 技能表 (moves.bytes + skill_effect.bytes):
    # 优先从 ConfigPackage 包里的游戏资源自解析, 数据完全来自游戏资源本身, 可随版本自更新;
    # 失败时回退到已有的 refs/monsters.json (仅 pet_attr)。
    try:
        cache_path = (pb_result or {}).get("cache_path")
        cfg_version = (pb_result or {}).get("version")
        if cache_path and Path(cache_path).exists():
            # 属性名映射 + 技能属性名共用: 优先从 skilltypes.bytes 自解析(含双属性/新属性)
            attr_names = PET_ATTR_NAMES
            try:
                stb = _get_asset_bytes(cache_path, SKILLTYPES_ASSET)
                if stb:
                    parsed = parse_skill_types(stb)
                    if parsed:
                        attr_names = parsed
                        log(f"属性名来自 skilltypes.bytes ({len(parsed)} 种)")
            except Exception as e:
                log(f"解析 skilltypes.bytes 属性名失败, 用内置映射: {e}")

            # 精灵属性表 pet_attr.json + 全物种名兜底表 monster_names.json
            # (两者同源 monsters.bytes, 合并一次读取; 各自独立记录版本)
            _st = _read_json(PET_ATTR_STATE)
            _mst = _read_json(MONSTER_NAMES_STATE)
            need_attr = not (not force
                             and _st.get("source") == "monsters.bytes+skilltypes.bytes"
                             and _st.get("version") == cfg_version and PET_ATTR_FILE.exists())
            need_names = not (not force
                              and _mst.get("source") == "monsters.bytes"
                              and _mst.get("version") == cfg_version
                              and MONSTER_NAMES_FILE.exists())
            if need_attr or need_names:
                mb = _get_asset_bytes(cache_path, MONSTERS_ASSET)
                if need_attr:
                    rr = regenerate_pet_attr_from_bytes(mb, attr_names=attr_names)
                    if rr.get("ok"):
                        _write_json_file(PET_ATTR_STATE, {
                            "version": cfg_version,
                            "source": "monsters.bytes+skilltypes.bytes",
                            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        })
                        log(f"pet_attr 已同步 monsters.bytes (版本 {cfg_version})")
                    else:
                        log(f"从 monsters.bytes 生成 pet_attr 失败, 回退 monsters.json: {rr.get('error')}")
                        regenerate_pet_attr()
                if need_names:
                    nr = regenerate_pet_names_from_bytes(mb)
                    if nr.get("ok"):
                        _write_json_file(MONSTER_NAMES_STATE, {
                            "version": cfg_version,
                            "source": "monsters.bytes",
                            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        })
                    else:
                        log(f"monster_names 生成失败: {nr.get('error')}")
            else:
                log(f"pet_attr / monster_names 已是最新 (版本 {cfg_version}), 跳过")

            # 技能表 skills.json (moves.bytes + skill_effect.bytes + 属性名)
            _ss = _read_json(SKILLS_STATE)
            if (not force and _ss.get("source") == "moves.bytes+skill_effect.bytes"
                    and _ss.get("version") == cfg_version and SKILLS_FILE.exists()):
                log(f"skills 已是最新 (版本 {cfg_version}), 跳过")
            else:
                try:
                    mvb = _get_asset_bytes(cache_path, MOVES_ASSET)
                    seb = _get_asset_bytes(cache_path, SKILL_EFFECT_ASSET)
                    sr = regenerate_skills(mvb, seb, type_names=attr_names)
                    if sr.get("ok"):
                        _write_json_file(SKILLS_STATE, {
                            "version": cfg_version,
                            "source": "moves.bytes+skill_effect.bytes",
                            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        })
                        log(f"skills 已同步 (版本 {cfg_version})")
                    else:
                        log(f"skills 生成失败: {sr.get('error')}")
                except Exception as e:
                    log(f"skills 更新失败: {e}")

            # 魂印表 soulmarks.json (专属特性: effecticon.bytes + effectag.bytes)
            _sm = _read_json(SOULMARKS_STATE)
            if (not force and _sm.get("source") == "effecticon.bytes+effectag.bytes"
                    and _sm.get("version") == cfg_version and SOULMARKS_FILE.exists()):
                log(f"soulmarks 已是最新 (版本 {cfg_version}), 跳过")
            else:
                try:
                    eib = _get_asset_bytes(cache_path, EFFECT_ICON_ASSET)
                    etb = _get_asset_bytes(cache_path, EFFECT_TAG_ASSET)
                    smr = regenerate_soulmarks(eib, etb)
                    if smr.get("ok"):
                        _write_json_file(SOULMARKS_STATE, {
                            "version": cfg_version,
                            "source": "effecticon.bytes+effectag.bytes",
                            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        })
                        log(f"soulmarks 已同步 (版本 {cfg_version})")
                    else:
                        log(f"soulmarks 生成失败: {smr.get('error')}")
                except Exception as e:
                    log(f"soulmarks 更新失败: {e}")

            # 物品名表 item_names.json (itemsoptimizecatitems{N}.bytes)
            _in = _read_json(ITEM_NAMES_STATE)
            if (not force and _in.get("source") == "itemsoptimizecatitems*.bytes"
                    and _in.get("version") == cfg_version and ITEM_NAMES_FILE.exists()):
                log(f"item_names 已是最新 (版本 {cfg_version}), 跳过")
            else:
                try:
                    inr = regenerate_item_names(cache_path)
                    if inr.get("ok"):
                        _write_json_file(ITEM_NAMES_STATE, {
                            "version": cfg_version,
                            "source": "itemsoptimizecatitems*.bytes",
                            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        })
                        log(f"item_names 已同步 (版本 {cfg_version})")
                    else:
                        log(f"item_names 生成失败: {inr.get('error')}")
                except Exception as e:
                    log(f"item_names 更新失败: {e}")
        else:
            log("未获得 ConfigPackage bundle, pet_attr 回退 monsters.json")
            regenerate_pet_attr()
    except Exception as e:
        log(f"pet_attr 更新未完成, 回退 monsters.json: {e}")
        try:
            regenerate_pet_attr()
        except Exception as e2:
            log(f"pet_attr 回退也失败: {e2}")

    # 魂印/效果图标 (DefaultPackage, 首次或 force 时下载+解出, 供 webui /effecticon/<id>.png)
    try:
        if force or not any(EFFECT_ICON_DIR.glob("*.png")):
            ensure_effect_icons()
    except Exception as e:
        log(f"effecticon 更新未完成: {e}")

    if not force and is_up_to_date(remote_version):
        log(f"已是最新 (版本 {remote_version}), 无需更新")
        return {"ok": True, "skipped": True, "version": remote_version, "error": None}

    log(f"检测到需要更新 (本地 {load_state().get('version')!r} -> 远端 {remote_version!r}), 开始更新...")

    # 先确保 UnityPy (自动安装)
    try:
        _ensure_unitypy()
    except UpdaterError as e:
        log(f"UnityPy 安装/导入失败: {e}")
        return {"ok": False, "skipped": True, "version": remote_version, "error": str(e)}

    manifest = get_remote_manifest()
    heads = _find_pet_head_bundles(manifest["bundles"])
    if not heads:
        log("清单里没有找到 pet_head_* bundle, 跳过更新")
        return {"ok": False, "skipped": True, "version": remote_version,
                "error": "清单中未发现 pet_head_* bundle"}

    # 1) 下载 (按大小增量)
    for name, fhash, fsize in heads:
        _download_bundle(name, fhash, fsize)
    log(f"已准备 {len(heads)} 个 pet_head bundle")

    # 2) 解析出头像
    _run_extractor()

    # 3) 记录版本
    save_state({
        "package": PKG,
        "version": remote_version,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "bundle_count": len(heads),
        "bundles": {name: {"hash": fh, "size": fs} for name, fh, fs in heads},
    })
    log(f"更新完成, 已记录版本 {remote_version}")
    return {"ok": True, "skipped": False, "version": remote_version, "error": None}


if __name__ == "__main__":
    force = "--force" in sys.argv
    r = ensure_pet_avatars(force=force)
    print(json.dumps(r, ensure_ascii=False, indent=2))
